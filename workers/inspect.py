"""
q_inspect worker — preflight / post_install / cleanup 단계별 태스크.

태스크 흐름:
  run_preflight → (sw_install stub) → run_post_install → validate_results
  validate_results → run_cleanup → generate_report

각 태스크는 SSH 오류 시 최대 3회 재시도 (20초 간격).
스크립트 실행 실패는 즉시 FAILED 처리 후 run_cleanup → generate_report.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncssh
import structlog
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings
from workers.app import app
from workers.notify import publish_job_status
from workers.ssh_client import secret_input, wrap_password

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 세션 팩토리
# ---------------------------------------------------------------------------


def _make_session() -> tuple:
    """매 asyncio.run() 루프마다 새 엔진+세션팩토리를 생성."""
    engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_local


# ---------------------------------------------------------------------------
# 경로 헬퍼
# ---------------------------------------------------------------------------


def _profile_path(profile_name: str) -> Path:
    return Path(__file__).parent.parent / "checks" / "profiles" / f"{profile_name}.json"


def _script_path(phase: str, script_name: str) -> Path:
    return Path(__file__).parent.parent / "checks" / "base" / phase / f"{script_name}.py"


def _nfs_raw_dir(job_id: str) -> Path:
    return Path(settings.nfs_base_path) / "results" / job_id / "inspect_raw"


def _ssh_key_path(target_host: str) -> str | None:
    key_dir = Path(settings.ssh_key_dir)
    for candidate in [key_dir / target_host, key_dir / "default"]:
        if candidate.exists():
            return str(candidate)
    return None


# ---------------------------------------------------------------------------
# DB 헬퍼
# ---------------------------------------------------------------------------


async def _update_job(session: AsyncSession, job_id: str, **kwargs) -> None:
    from api.models import Job

    result = await session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    job = result.scalar_one_or_none()
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    for k, v in kwargs.items():
        setattr(job, k, v)
    job.updated_at = datetime.now(timezone.utc)
    await session.commit()


async def _save_check_result(
    session: AsyncSession,
    job_id: str,
    check_name: str,
    status: str,
    detail: str,
    raw_output: dict,
) -> None:
    from api.models import CheckResult

    cr = CheckResult(
        job_id=uuid.UUID(job_id),
        check_name=check_name,
        status=status,
        detail=detail,
        raw_output=raw_output,
    )
    session.add(cr)
    await session.commit()


async def _mark_failed(job_id: str, message: str) -> None:
    engine, SessionLocal = _make_session()
    try:
        async with SessionLocal() as session:
            await _update_job(session, job_id, status="failed", error_message=message[:2000])
        await publish_job_status(job_id, "failed", message[:2000])
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# SSH 헬퍼
# ---------------------------------------------------------------------------


def _build_connect_kwargs(target_host: str, target_user: str) -> dict:
    kwargs: dict = {
        "host": target_host,
        "username": target_user,
        "known_hosts": None,  # TODO(W-3): known_hosts 명시 경로로 교체
    }
    key_path = _ssh_key_path(target_host)
    if key_path:
        kwargs["client_keys"] = [key_path]
    return kwargs


async def _apt_install(
    conn: asyncssh.SSHClientConnection,
    packages: list[str],
    secret: SecretStr,
    timeout: int = 300,
) -> tuple[bool, str]:
    pkg_str = " ".join(packages)
    cmd = f"DEBIAN_FRONTEND=noninteractive sudo -S apt-get install -y {pkg_str} 2>&1"
    result = await conn.run(
        cmd,
        input=secret_input(secret),
        check=False,
        timeout=timeout,
    )
    return result.exit_status == 0, (result.stdout or "").strip()


async def _run_script(
    conn: asyncssh.SSHClientConnection,
    local_script: Path,
    remote_tmp: str,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> asyncssh.SSHCompletedProcess:
    remote_path = f"{remote_tmp}/{local_script.name}"
    async with conn.start_sftp_client() as sftp:
        await sftp.put(str(local_script), remote_path)
        await sftp.chmod(remote_path, 0o755)
    return await conn.run(f"python3 {remote_path}", env=env or {}, check=False, timeout=timeout)


# ---------------------------------------------------------------------------
# 공통 단계 실행 로직
# ---------------------------------------------------------------------------


async def _run_phase_scripts(
    conn: asyncssh.SSHClientConnection,
    job_id: str,
    phase: str,
    scripts: list[str],
    phase_env: dict[str, str],
    timeout: int,
    raw_dir: Path,
    SessionLocal: async_sessionmaker,
) -> bool:
    """phase 내 스크립트를 순차 실행. 스크립트 실행 실패 시 False 반환."""
    for script_name in scripts:
        local_script = _script_path(phase, script_name)
        if not local_script.exists():
            log.warning("script.missing", script=script_name, phase=phase)
            async with SessionLocal() as session:
                await _save_check_result(
                    session,
                    job_id,
                    script_name,
                    "warn",
                    f"script not found: {local_script.name}",
                    {},
                )
            continue

        log.info("script.run", script=script_name, phase=phase)
        try:
            result = await _run_script(
                conn,
                local_script,
                f"/tmp/inspection_{job_id[:8]}",
                env=phase_env,
                timeout=timeout,
            )
        except (asyncssh.misc.DisconnectError, TimeoutError) as exc:
            detail = f"script timeout/disconnect after {timeout}s: {exc}"
            log.warning("script.timeout", script=script_name)
            async with SessionLocal() as session:
                await _save_check_result(
                    session,
                    job_id,
                    script_name,
                    "warn",
                    detail,
                    {"check": script_name, "status": "warn", "detail": detail},
                )
            continue

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.exit_status != 0:
            detail = f"exit_code={result.exit_status} stderr={stderr[:200]}"
            log.error(
                "script.failed",
                script=script_name,
                exit_code=result.exit_status,
                error_type="script_execution_error",
                stage=phase,
            )
            async with SessionLocal() as session:
                await _save_check_result(
                    session,
                    job_id,
                    script_name,
                    "fail",
                    detail,
                    {"check": script_name, "status": "fail", "detail": detail},
                )
            return False

        try:
            output: dict = json.loads(stdout)
        except json.JSONDecodeError:
            log.warning("script.bad_json", script=script_name, stdout=stdout[:200])
            output = {
                "check": script_name,
                "status": "fail",
                "detail": f"JSON parse error. stdout={stdout[:200]}",
            }

        status = output.get("status", "fail")
        detail = output.get("detail", "")
        check_name = output.get("check", script_name)

        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{script_name}.json").write_text(
            json.dumps(output, ensure_ascii=False, indent=2)
        )

        async with SessionLocal() as session:
            await _save_check_result(session, job_id, check_name, status, detail, output)

        log.info("script.done", script=script_name, status=status)

    return True


# ---------------------------------------------------------------------------
# run_preflight
# ---------------------------------------------------------------------------


async def _async_preflight(
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
    sudo_password: str | None,
) -> None:
    secret = wrap_password(sudo_password)
    engine, SessionLocal = _make_session()
    try:
        profile_file = _profile_path(product_profile)
        if not profile_file.exists():
            raise FileNotFoundError(f"Profile not found: {profile_file}")
        with profile_file.open() as f:
            profile: dict = json.load(f)

        raw_dir = _nfs_raw_dir(job_id)
        raw_dir.mkdir(parents=True, exist_ok=True)

        async with SessionLocal() as session:
            await _update_job(session, job_id, status="preflight")
        await publish_job_status(job_id, "preflight")

        connect_kwargs = _build_connect_kwargs(target_host, target_user)
        log.info("preflight.ssh_connect", host=target_host)

        async with asyncssh.connect(**connect_kwargs) as conn:
            remote_tmp = f"/tmp/inspection_{job_id[:8]}"
            await conn.run(f"mkdir -p {remote_tmp}", check=True)

            # baseline 패키지 설치
            baseline = profile.get("pre_install", {}).get("baseline", [])
            if baseline and secret:
                log.info("preflight.baseline_install", packages=baseline)
                ok, output = await _apt_install(conn, baseline, secret)
                if not ok:
                    log.warning("preflight.baseline_failed", output=output[:200])
            elif baseline and not secret:
                log.warning("preflight.baseline_skip", reason="sudo_password not provided")

            phase_cfg = profile.get("phases", {}).get("preflight", {})
            scripts: list[str] = phase_cfg.get("scripts", [])
            phase_env: dict[str, str] = dict(phase_cfg.get("env", {}))
            if secret:
                phase_env["SUDO_PASSWORD"] = secret.get_secret_value()

            success = await _run_phase_scripts(
                conn,
                job_id,
                "preflight",
                scripts,
                phase_env,
                int(phase_cfg.get("timeout", 300)),
                raw_dir,
                SessionLocal,
            )

            await conn.run(f"rm -rf {remote_tmp}", check=False)

        if not success:
            await _mark_failed(job_id, "preflight script execution failed")
            _dispatch_cleanup(job_id, target_host, target_user, product_profile, sudo_password)
            return

        # sw_requirements 유무에 따라 분기
        async with SessionLocal() as session:
            from api.models import Job

            result = await session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
            job = result.scalar_one_or_none()
            has_sw_req = bool(job and job.sw_requirements)

        if has_sw_req:
            # C-5 미구현: sw_install 단계 stub — 경고 후 post_install으로 fallthrough
            log.warning(
                "preflight.sw_install_stub",
                job_id=job_id,
                msg="sw_install not yet implemented (C-5), skipping to post_install",
            )

        run_post_install.apply_async(
            args=[job_id, target_host, target_user, product_profile],
            kwargs={"sudo_password": sudo_password},
            queue="q_inspect",
        )
        log.info("preflight.done", job_id=job_id)

    finally:
        await engine.dispose()


@app.task(
    bind=True,
    queue="q_inspect",
    acks_late=True,
    max_retries=3,
    default_retry_delay=20,
    soft_time_limit=7200,
    time_limit=7500,
    name="workers.inspect.run_preflight",
)
def run_preflight(
    self,
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
    sudo_password: str | None = None,
) -> dict:
    log.info("preflight.start", job_id=job_id, host=target_host, profile=product_profile)
    try:
        asyncio.run(
            _async_preflight(job_id, target_host, target_user, product_profile, sudo_password)
        )
        return {"job_id": job_id, "phase": "preflight", "result": "ok"}
    except asyncssh.DisconnectError as exc:
        asyncio.run(_mark_failed(job_id, f"SSH disconnect: {exc}"))
        raise self.retry(exc=exc)
    except asyncssh.PermissionDenied as exc:
        asyncio.run(_mark_failed(job_id, f"SSH auth failed: {exc}"))
        raise
    except FileNotFoundError as exc:
        asyncio.run(_mark_failed(job_id, str(exc)))
        raise
    except Exception as exc:
        asyncio.run(_mark_failed(job_id, str(exc)))
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# run_post_install
# ---------------------------------------------------------------------------


async def _async_post_install(
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
    sudo_password: str | None,
) -> None:
    secret = wrap_password(sudo_password)
    engine, SessionLocal = _make_session()
    try:
        profile_file = _profile_path(product_profile)
        with profile_file.open() as f:
            profile: dict = json.load(f)

        raw_dir = _nfs_raw_dir(job_id)

        async with SessionLocal() as session:
            await _update_job(session, job_id, status="post_install")
        await publish_job_status(job_id, "post_install")

        connect_kwargs = _build_connect_kwargs(target_host, target_user)
        log.info("post_install.ssh_connect", host=target_host)

        async with asyncssh.connect(**connect_kwargs) as conn:
            remote_tmp = f"/tmp/inspection_{job_id[:8]}"
            await conn.run(f"mkdir -p {remote_tmp}", check=True)

            # stress_tools 설치
            stress_tools = profile.get("pre_install", {}).get("stress_tools", [])
            if stress_tools and secret:
                log.info("post_install.stress_tools_install", packages=stress_tools)
                ok, output = await _apt_install(conn, stress_tools, secret, timeout=120)
                if not ok:
                    log.warning("post_install.stress_tools_failed", output=output[:200])
            elif stress_tools and not secret:
                log.warning("post_install.stress_tools_skip", reason="sudo_password not provided")

            phase_cfg = profile.get("phases", {}).get("post_install", {})
            scripts: list[str] = phase_cfg.get("scripts", [])
            phase_env: dict[str, str] = dict(phase_cfg.get("env", {}))
            if secret:
                phase_env["SUDO_PASSWORD"] = secret.get_secret_value()

            success = await _run_phase_scripts(
                conn,
                job_id,
                "post_install",
                scripts,
                phase_env,
                int(phase_cfg.get("timeout", 7200)),
                raw_dir,
                SessionLocal,
            )

            await conn.run(f"rm -rf {remote_tmp}", check=False)

        if not success:
            await _mark_failed(job_id, "post_install script execution failed")
            _dispatch_cleanup(job_id, target_host, target_user, product_profile, sudo_password)
            return

        run_collect.apply_async(
            args=[job_id, target_host, target_user, product_profile],
            kwargs={"sudo_password": sudo_password},
            queue="q_inspect",
        )
        log.info("post_install.done", job_id=job_id)

    finally:
        await engine.dispose()


@app.task(
    bind=True,
    queue="q_inspect",
    acks_late=True,
    max_retries=3,
    default_retry_delay=20,
    soft_time_limit=7200,
    time_limit=7500,
    name="workers.inspect.run_post_install",
)
def run_post_install(
    self,
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
    sudo_password: str | None = None,
) -> dict:
    log.info("post_install.start", job_id=job_id, host=target_host)
    try:
        asyncio.run(
            _async_post_install(job_id, target_host, target_user, product_profile, sudo_password)
        )
        return {"job_id": job_id, "phase": "post_install", "result": "ok"}
    except asyncssh.DisconnectError as exc:
        asyncio.run(_mark_failed(job_id, f"SSH disconnect: {exc}"))
        raise self.retry(exc=exc)
    except asyncssh.PermissionDenied as exc:
        asyncio.run(_mark_failed(job_id, f"SSH auth failed: {exc}"))
        raise
    except Exception as exc:
        asyncio.run(_mark_failed(job_id, str(exc)))
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# run_collect
# ---------------------------------------------------------------------------


async def _async_collect(
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
    sudo_password: str | None,
) -> None:
    secret = wrap_password(sudo_password)
    engine, SessionLocal = _make_session()
    try:
        profile_file = _profile_path(product_profile)
        with profile_file.open() as f:
            profile: dict = json.load(f)

        raw_dir = _nfs_raw_dir(job_id)
        phase_cfg = profile.get("phases", {}).get("collect", {})
        scripts: list[str] = phase_cfg.get("scripts", [])
        phase_env: dict[str, str] = dict(phase_cfg.get("env", {}))
        if secret:
            phase_env["SUDO_PASSWORD"] = secret.get_secret_value()

        if scripts:
            connect_kwargs = _build_connect_kwargs(target_host, target_user)
            log.info("collect.ssh_connect", host=target_host)
            try:
                async with asyncssh.connect(**connect_kwargs) as conn:
                    remote_tmp = f"/tmp/inspection_{job_id[:8]}"
                    await conn.run(f"mkdir -p {remote_tmp}", check=True)
                    await _run_phase_scripts(
                        conn,
                        job_id,
                        "collect",
                        scripts,
                        phase_env,
                        int(phase_cfg.get("timeout", 120)),
                        raw_dir,
                        SessionLocal,
                    )
                    await conn.run(f"rm -rf {remote_tmp}", check=False)
            except Exception as exc:
                # collect 실패는 non-fatal — 로그 수집 실패로 검수 결과에 영향 없음
                log.warning("collect.failed_warn", error=str(exc))

        log.info("collect.done", job_id=job_id)

        # collect 완료 → validate dispatch
        async with SessionLocal() as session:
            result_path = str(_nfs_raw_dir(job_id))
            await _update_job(session, job_id, status="validating", result_path=result_path)
        await publish_job_status(job_id, "validating")

        from workers.validate import validate_results

        validate_results.apply_async(
            args=[job_id],
            kwargs={
                "sudo_password": sudo_password,
                "target_host": target_host,
                "target_user": target_user,
                "product_profile": product_profile,
            },
            queue="q_validate",
        )
    finally:
        await engine.dispose()


@app.task(
    bind=True,
    queue="q_inspect",
    acks_late=True,
    max_retries=1,
    default_retry_delay=20,
    name="workers.inspect.run_collect",
)
def run_collect(
    self,
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
    sudo_password: str | None = None,
) -> dict:
    log.info("collect.start", job_id=job_id)
    try:
        asyncio.run(
            _async_collect(job_id, target_host, target_user, product_profile, sudo_password)
        )
        return {"job_id": job_id, "phase": "collect", "result": "ok"}
    except asyncssh.DisconnectError as exc:
        raise self.retry(exc=exc)
    except Exception as exc:
        log.error("collect.unexpected_error", error=str(exc))
        asyncio.run(_mark_failed(job_id, str(exc)))
        _dispatch_cleanup(job_id, target_host, target_user, product_profile, sudo_password)
        raise


# ---------------------------------------------------------------------------
# run_cleanup
# ---------------------------------------------------------------------------


async def _async_cleanup(
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
    sudo_password: str | None,
) -> None:
    secret = wrap_password(sudo_password)
    engine, SessionLocal = _make_session()
    try:
        profile_file = _profile_path(product_profile)
        with profile_file.open() as f:
            profile: dict = json.load(f)

        cleanup_cfg: dict = profile.get("cleanup", {})
        on_failure = cleanup_cfg.get("on_failure", "warn")

        async with SessionLocal() as session:
            await _update_job(session, job_id, status="cleanup")
        await publish_job_status(job_id, "cleanup")

        connect_kwargs = _build_connect_kwargs(target_host, target_user)
        log.info("cleanup.ssh_connect", host=target_host)

        try:
            async with asyncssh.connect(**connect_kwargs) as conn:
                # 패키지 제거
                remove_packages: list[str] = cleanup_cfg.get("remove_packages", [])
                if remove_packages and secret:
                    pkg_str = " ".join(remove_packages)
                    cmd = f"sudo -S apt-get remove -y {pkg_str} 2>&1"
                    result = await conn.run(
                        cmd,
                        input=secret_input(secret),
                        check=False,
                        timeout=120,
                    )
                    if result.exit_status != 0:
                        log.warning(
                            "cleanup.pkg_remove_failed",
                            packages=remove_packages,
                            stderr=(result.stderr or "")[:200],
                        )
                elif remove_packages and not secret:
                    log.warning("cleanup.pkg_skip", reason="sudo_password not provided")

                # 디렉토리 제거
                for dir_path in cleanup_cfg.get("remove_dirs", []):
                    if not dir_path.startswith("/opt/"):
                        log.warning("cleanup.dir_skip", path=dir_path, reason="not under /opt/")
                        continue
                    result = await conn.run(
                        f"sudo -S rm -rf {dir_path}",
                        input=secret_input(secret),
                        check=False,
                        timeout=30,
                    )
                    if result.exit_status != 0:
                        log.warning("cleanup.dir_remove_failed", path=dir_path)

        except Exception as exc:
            if on_failure == "warn":
                log.warning("cleanup.failed_warn", error=str(exc))
            else:
                raise

        # cleanup 완료 → report 트리거
        async with SessionLocal() as session:
            await _update_job(session, job_id, status="reporting")
        await publish_job_status(job_id, "reporting")

        from workers.report import generate_report

        generate_report.apply_async(args=[job_id], queue="q_report")
        log.info("cleanup.done", job_id=job_id)

    finally:
        await engine.dispose()


@app.task(
    bind=True,
    queue="q_inspect",
    acks_late=True,
    max_retries=1,
    default_retry_delay=20,
    name="workers.inspect.run_cleanup",
)
def run_cleanup(
    self,
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
    sudo_password: str | None = None,
) -> dict:
    log.info("cleanup.start", job_id=job_id)
    try:
        asyncio.run(
            _async_cleanup(job_id, target_host, target_user, product_profile, sudo_password)
        )
        return {"job_id": job_id, "phase": "cleanup", "result": "ok"}
    except Exception as exc:
        # cleanup 실패는 on_failure=warn이 내부에서 처리됨
        # 이 경로는 예상 밖 오류
        log.error("cleanup.unexpected_error", error=str(exc))
        # 재시도 소진 후에만 report 트리거 — 재시도 성공 시 중복 dispatch 방지
        if self.request.retries >= self.max_retries:
            asyncio.run(_mark_report_trigger(job_id))
        raise self.retry(exc=exc)


async def _mark_report_trigger(job_id: str) -> None:
    """cleanup 예상 밖 실패 시 report는 그대도 트리거."""
    engine, SessionLocal = _make_session()
    try:
        async with SessionLocal() as session:
            await _update_job(session, job_id, status="reporting")
        await publish_job_status(job_id, "reporting")
        from workers.report import generate_report

        generate_report.apply_async(args=[job_id], queue="q_report")
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 헬퍼: cleanup dispatch (실패 시 항상 호출)
# ---------------------------------------------------------------------------


def _dispatch_cleanup(
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
    sudo_password: str | None,
) -> None:
    run_cleanup.apply_async(
        args=[job_id, target_host, target_user, product_profile],
        kwargs={"sudo_password": sudo_password},
        queue="q_inspect",
    )

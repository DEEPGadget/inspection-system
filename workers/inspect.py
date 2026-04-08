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
from workers.ssh_client import wrap_password

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


def _build_connect_kwargs(target_host: str, target_user: str, password: str | None = None) -> dict:
    kwargs: dict = {
        "host": target_host,
        "username": target_user,
        "known_hosts": None,  # TODO(W-3): known_hosts 명시 경로로 교체
    }
    if password:
        kwargs["password"] = password
    else:
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
        input=secret.get_secret_value() if secret else None,
        check=False,
        timeout=timeout,
    )
    return result.exit_status == 0, (result.stdout or "").strip()


async def _verify_packages(
    conn: asyncssh.SSHClientConnection,
    packages: list[str],
) -> tuple[list[str], list[str]]:
    """패키지별 dpkg -s 로 설치 여부 확인. (installed, missing) 반환."""
    installed: list[str] = []
    missing: list[str] = []
    for pkg in packages:
        # dpkg -s: 미설치 시 exit≠0 / 설치 시 'Status: install ok installed' 라인 포함
        result = await conn.run(
            f"dpkg -s {pkg} 2>/dev/null | grep -q '^Status: install ok installed'",
            check=False,
            timeout=15,
        )
        if result.exit_status == 0:
            installed.append(pkg)
        else:
            missing.append(pkg)
    return installed, missing


async def _install_and_verify_baseline(
    conn: asyncssh.SSHClientConnection,
    packages: list[str],
    secret: SecretStr,
    timeout: int = 300,
) -> tuple[bool, list[str], list[str], str]:
    """baseline 설치 + 검증. (ok, installed, missing, apt_output_tail) 반환."""
    _, apt_out = await _apt_install(conn, packages, secret, timeout=timeout)
    installed, missing = await _verify_packages(conn, packages)
    # apt 출력 마지막 5줄만 보존 (민감정보 회피 + 컨텍스트)
    tail = " // ".join([line for line in (apt_out or "").splitlines() if line.strip()][-5:])
    return (len(missing) == 0), installed, missing, tail


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

        connect_kwargs = _build_connect_kwargs(
            target_host, target_user, secret.get_secret_value() if secret else None
        )
        log.info("preflight.ssh_connect", host=target_host)

        phase_cfg = profile.get("phases", {}).get("preflight", {})
        scripts: list[str] = phase_cfg.get("scripts", [])
        phase_env: dict[str, str] = dict(phase_cfg.get("env", {}))
        if secret:
            phase_env["SUDO_PASSWORD"] = secret.get_secret_value()

        stress_cfg = profile.get("stress_config", {})
        driver_version = stress_cfg.get("driver_version", "580")

        reboot_needed = False  # GRUB 변경 또는 driver 임시 설치로 인한 재부팅 여부
        reboot_triggered = False  # _do_reboot 실제 완료 여부 (네트워크 오류 구분용)
        temp_packages: list[str] = []
        success = False

        # ── 세션 1: baseline + sys-config + 임시 driver 설치 ─────────────────
        try:
            async with asyncssh.connect(**connect_kwargs) as conn:
                remote_tmp = f"/tmp/inspection_{job_id[:8]}"
                await conn.run(f"mkdir -p {remote_tmp}", check=True)

                # baseline 패키지 설치 + 패키지별 검증
                baseline = profile.get("pre_install", {}).get("baseline", [])
                baseline_failed_msg: str | None = None
                if baseline and secret:
                    log.info("preflight.baseline_install", packages=baseline)
                    ok, installed, missing, apt_tail = await _install_and_verify_baseline(
                        conn, baseline, secret
                    )
                    if not ok:
                        log.error(
                            "preflight.baseline_failed",
                            installed=installed,
                            missing=missing,
                            apt_tail=apt_tail[:500],
                        )
                        # check_results에 사유 기록
                        async with SessionLocal() as session:
                            await _save_check_result(
                                session,
                                job_id,
                                check_name="baseline_install",
                                status="fail",
                                detail=(
                                    f"installed={','.join(installed) or 'none'}|"
                                    f"missing={','.join(missing)}|"
                                    f"apt_tail={apt_tail[:300]}"
                                ),
                                raw_output={
                                    "installed": installed,
                                    "missing": missing,
                                    "apt_tail": apt_tail[:2000],
                                },
                            )
                        baseline_failed_msg = (
                            f"baseline install failed — missing: {','.join(missing)}"
                        )
                elif baseline and not secret:
                    log.error("preflight.baseline_skip", reason="sudo_password not provided")
                    async with SessionLocal() as session:
                        await _save_check_result(
                            session,
                            job_id,
                            check_name="baseline_install",
                            status="fail",
                            detail=(
                                f"missing={','.join(baseline)}|reason=sudo_password not provided"
                            ),
                            raw_output={
                                "installed": [],
                                "missing": baseline,
                                "reason": "sudo_password not provided",
                            },
                        )
                    baseline_failed_msg = "baseline install skipped: sudo_password not provided"

                if baseline_failed_msg:
                    await _mark_failed(job_id, baseline_failed_msg)
                    _dispatch_cleanup(
                        job_id, target_host, target_user, product_profile, sudo_password
                    )
                    return

                # sys-config 전체 적용 (sw_requirements 유무 무관)
                from workers.sw_install import (
                    _apply_sys_config,
                    _check_driver_installed,
                    _detect_cpu_vendor,
                    _detect_has_nvidia_gpu,
                    _detect_os,
                    _do_reboot,
                    _install_nvidia_driver,
                    _poll_ssh_reconnect,
                    _save_temp_packages,
                )

                os_info = await _detect_os(conn)
                if os_info.get("id") == "ubuntu":
                    cpu_vendor = await _detect_cpu_vendor(conn)
                    has_nvidia = await _detect_has_nvidia_gpu(conn)
                    log.info(
                        "preflight.sys_config",
                        cpu_vendor=cpu_vendor,
                        has_nvidia=has_nvidia,
                    )
                    sys_results = await _apply_sys_config(conn, secret, cpu_vendor, has_nvidia)
                    grub_updated = any(
                        r.get("name") == "sys_config_grub" and "grub_updated" in r.get("detail", "")
                        for r in sys_results
                    )
                    if grub_updated:
                        reboot_needed = True
                        log.info("preflight.grub_update.pending_reboot", job_id=job_id)

                    # GPU 있고 driver 미설치 → 임시 설치 (재부팅은 아래에서 통합)
                    if has_nvidia and secret:
                        driver_ok = await _check_driver_installed(conn)
                        if not driver_ok:
                            log.info(
                                "preflight.temp_driver_install",
                                version=driver_version,
                                job_id=job_id,
                            )
                            ok, out = await _install_nvidia_driver(
                                conn, secret, driver_version, os_info
                            )
                            if ok:
                                temp_packages.append(f"nvidia-driver-{driver_version}")
                                _save_temp_packages(job_id, temp_packages)
                                reboot_needed = True
                                log.info(
                                    "preflight.temp_driver_installed",
                                    pkg=f"nvidia-driver-{driver_version}",
                                )
                            else:
                                log.warning("preflight.temp_driver_failed", out=out[:200])
                else:
                    log.info(
                        "preflight.sys_config.skip",
                        reason="non-ubuntu",
                        os_id=os_info.get("id"),
                    )

                # 재부팅 필요 없으면 스크립트를 같은 세션에서 실행
                if not reboot_needed:
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
                else:
                    # GRUB + driver 설치를 단일 재부팅으로 통합
                    log.info("preflight.single_reboot.trigger", job_id=job_id)
                    await _do_reboot(conn, secret)
                    reboot_triggered = True

        except asyncssh.DisconnectError:
            if not reboot_triggered:
                raise  # 예상치 못한 연결 끊김 (reboot 미발생 상태)

        # ── 재부팅 후 세션 2: preflight 스크립트 실행 ────────────────────────
        if reboot_needed:
            async with SessionLocal() as session:
                await _update_job(session, job_id, status="rebooting")
            await publish_job_status(job_id, "rebooting")
            log.info("preflight.reboot.waiting", job_id=job_id)
            new_conn = await _poll_ssh_reconnect(connect_kwargs, timeout=300, interval=10)
            if new_conn is None:
                await _mark_failed(
                    job_id,
                    "reboot_timeout: server did not respond within 300s after reboot",
                )
                _dispatch_cleanup(job_id, target_host, target_user, product_profile, sudo_password)
                return

            async with new_conn:
                remote_tmp = f"/tmp/inspection_{job_id[:8]}"
                await new_conn.run(f"mkdir -p {remote_tmp}", check=True)
                success = await _run_phase_scripts(
                    new_conn,
                    job_id,
                    "preflight",
                    scripts,
                    phase_env,
                    int(phase_cfg.get("timeout", 300)),
                    raw_dir,
                    SessionLocal,
                )
                await new_conn.run(f"rm -rf {remote_tmp}", check=False)

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
            from workers.sw_install import run_sw_install

            run_sw_install.apply_async(
                args=[job_id, target_host, target_user, product_profile],
                kwargs={"sudo_password": sudo_password},
                queue="q_sw_install",
            )
        else:
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
        if self.request.retries >= self.max_retries:
            asyncio.run(_mark_failed(job_id, f"SSH disconnect: {exc}"))
        raise self.retry(exc=exc)
    except asyncssh.PermissionDenied as exc:
        asyncio.run(_mark_failed(job_id, f"SSH auth failed: {exc}"))
        raise
    except FileNotFoundError as exc:
        asyncio.run(_mark_failed(job_id, str(exc)))
        raise
    except Exception as exc:
        if self.request.retries >= self.max_retries:
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

        connect_kwargs = _build_connect_kwargs(
            target_host, target_user, secret.get_secret_value() if secret else None
        )
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

            # GPU 있고 CUDA 미설치 → 임시 CUDA 설치 (stress_gpu.py gpu_burn 실행 전제)
            from workers.sw_install import (
                _check_cuda_installed,
                _detect_has_nvidia_gpu,
                _install_cuda,
                _load_temp_packages,
                _save_temp_packages,
            )

            stress_cfg = profile.get("stress_config", {})
            cuda_version = stress_cfg.get("cuda_version", "13")
            has_nvidia = await _detect_has_nvidia_gpu(conn)
            if has_nvidia and secret:
                cuda_ok = await _check_cuda_installed(conn)
                if not cuda_ok:
                    log.info(
                        "post_install.temp_cuda_install",
                        version=cuda_version,
                        job_id=job_id,
                    )
                    ok, out = await _install_cuda(conn, secret, cuda_version)
                    if ok:
                        ver_str = cuda_version.replace(".", "-")
                        pkg = f"cuda-toolkit-{ver_str}"
                        temp_pkgs = _load_temp_packages(job_id)
                        temp_pkgs.append(pkg)
                        _save_temp_packages(job_id, temp_pkgs)
                        log.info("post_install.temp_cuda_installed", pkg=pkg)
                    else:
                        log.warning("post_install.temp_cuda_failed", out=out[:200])

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
        if self.request.retries >= self.max_retries:
            asyncio.run(_mark_failed(job_id, f"SSH disconnect: {exc}"))
        raise self.retry(exc=exc)
    except asyncssh.PermissionDenied as exc:
        asyncio.run(_mark_failed(job_id, f"SSH auth failed: {exc}"))
        raise
    except Exception as exc:
        if self.request.retries >= self.max_retries:
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
            connect_kwargs = _build_connect_kwargs(
                target_host, target_user, secret.get_secret_value() if secret else None
            )
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

        connect_kwargs = _build_connect_kwargs(
            target_host, target_user, secret.get_secret_value() if secret else None
        )
        log.info("cleanup.ssh_connect", host=target_host)

        from workers.sw_install import _load_temp_packages

        temp_packages = _load_temp_packages(job_id)

        try:
            async with asyncssh.connect(**connect_kwargs) as conn:
                # 프로파일 지정 패키지 제거
                remove_packages: list[str] = cleanup_cfg.get("remove_packages", [])
                if remove_packages and secret:
                    pkg_str = " ".join(remove_packages)
                    cmd = f"sudo -S apt-get remove -y {pkg_str} 2>&1"
                    result = await conn.run(
                        cmd,
                        input=secret.get_secret_value() if secret else None,
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

                # 임시 설치 패키지 제거 (driver/CUDA)
                if temp_packages and secret:
                    pkg_str = " ".join(temp_packages)
                    cmd = f"DEBIAN_FRONTEND=noninteractive sudo -S apt-get purge -y {pkg_str} 2>&1"
                    result = await conn.run(
                        cmd,
                        input=secret.get_secret_value() if secret else None,
                        check=False,
                        timeout=300,
                    )
                    if result.exit_status != 0:
                        log.warning(
                            "cleanup.temp_pkg_remove_failed",
                            packages=temp_packages,
                            stderr=(result.stderr or "")[:200],
                        )
                    else:
                        log.info("cleanup.temp_pkg_removed", packages=temp_packages)
                elif temp_packages and not secret:
                    log.warning("cleanup.temp_pkg_skip", reason="sudo_password not provided")

                # 디렉토리 제거
                # 허용 prefix: /opt/ (sudo 필요), $HOME/ 또는 ~/ (sudo 불필요)
                for dir_path in cleanup_cfg.get("remove_dirs", []):
                    is_opt = dir_path.startswith("/opt/")
                    is_home = dir_path.startswith("$HOME/") or dir_path.startswith("~/")
                    if not (is_opt or is_home):
                        log.warning(
                            "cleanup.dir_skip",
                            path=dir_path,
                            reason="not under /opt/ or $HOME/",
                        )
                        continue
                    if is_opt:
                        cmd = f"sudo -S rm -rf {dir_path}"
                        stdin_input = secret.get_secret_value() if secret else None
                    else:
                        cmd = f"rm -rf {dir_path}"
                        stdin_input = None
                    result = await conn.run(
                        cmd,
                        input=stdin_input,
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

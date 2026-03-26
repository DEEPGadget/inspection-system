"""
q_inspect worker — SSH 접속 후 검수 스크립트 실행, 결과를 NFS + DB에 저장.
Celery task(sync) 내부에서 asyncio.run()으로 asyncssh 구동.
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncssh
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings
from workers.app import app

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# 내부 DB 세션 (worker 전용)
# ---------------------------------------------------------------------------
_engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
_SessionLocal = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _profile_path(profile_name: str) -> Path:
    base = Path(__file__).parent.parent / "checks" / "profiles"
    return base / f"{profile_name}.json"


def _script_path(phase_dir: str, script_name: str) -> Path:
    base = Path(__file__).parent.parent / "checks" / "base"
    return base / phase_dir / f"{script_name}.sh"


def _nfs_raw_dir(job_id: str) -> Path:
    return Path(settings.nfs_base_path) / "results" / job_id / "inspect_raw"


def _ssh_key_path(target_host: str) -> str | None:
    """호스트 전용 키 → 없으면 default 키 → 없으면 None(agent 사용)."""
    key_dir = Path(settings.ssh_key_dir)
    for candidate in [key_dir / target_host, key_dir / "default"]:
        if candidate.exists():
            return str(candidate)
    return None


# ---------------------------------------------------------------------------
# async 핵심 로직
# ---------------------------------------------------------------------------

async def _update_job(session: AsyncSession, job_id: str, **kwargs) -> None:
    from api.models import Job  # 순환 import 방지

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


async def _run_script_over_ssh(
    conn: asyncssh.SSHClientConnection,
    local_script: Path,
    remote_tmp: str,
    env: dict[str, str] | None = None,
) -> asyncssh.SSHCompletedProcess:
    """스크립트를 원격 /tmp에 업로드한 뒤 실행, 결과 반환."""
    remote_path = f"{remote_tmp}/{local_script.name}"
    async with conn.start_sftp_client() as sftp:
        await sftp.put(str(local_script), remote_path)
        await sftp.chmod(remote_path, 0o755)

    cmd = f"bash {remote_path}"
    result = await conn.run(cmd, env=env or {}, check=False)
    return result


async def _async_inspect(
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
) -> None:
    # ---- 프로파일 로드 ----
    profile_file = _profile_path(product_profile)
    if not profile_file.exists():
        raise FileNotFoundError(f"Profile not found: {profile_file}")
    with profile_file.open() as f:
        profile: dict = json.load(f)

    phases: dict = profile.get("phases", {})

    # ---- NFS 결과 디렉토리 생성 ----
    raw_dir = _nfs_raw_dir(job_id)
    raw_dir.mkdir(parents=True, exist_ok=True)

    async with _SessionLocal() as session:
        await _update_job(session, job_id, status="inspecting")

    # ---- SSH 접속 ----
    connect_kwargs: dict = {
        "host": target_host,
        "username": target_user,
        "known_hosts": None,  # TODO: known_hosts 검증 추가 시 변경
    }
    key_path = _ssh_key_path(target_host)
    if key_path:
        connect_kwargs["client_keys"] = [key_path]

    log.info("ssh.connect", host=target_host, user=target_user, key=key_path)

    async with asyncssh.connect(**connect_kwargs) as conn:
        remote_tmp = f"/tmp/inspection_{job_id[:8]}"
        await conn.run(f"mkdir -p {remote_tmp}", check=True)

        try:
            for phase_dir, phase_cfg in phases.items():
                if not phase_cfg.get("enabled", False):
                    log.debug("phase.skip", phase=phase_dir)
                    continue

                scripts: list[str] = phase_cfg.get("scripts", [])
                for script_name in scripts:
                    local_script = _script_path(phase_dir, script_name)
                    if not local_script.exists():
                        log.warning("script.missing", script=str(local_script))
                        async with _SessionLocal() as session:
                            await _save_check_result(
                                session, job_id, script_name, "warn",
                                f"script not found: {local_script.name}", {}
                            )
                        continue

                    log.info("script.run", script=script_name, phase=phase_dir)
                    result = await _run_script_over_ssh(conn, local_script, remote_tmp)

                    # stdout → JSON 파싱
                    stdout = result.stdout.strip() if result.stdout else ""
                    stderr = result.stderr.strip() if result.stderr else ""

                    try:
                        output: dict = json.loads(stdout)
                    except json.JSONDecodeError:
                        log.warning(
                            "script.bad_json",
                            script=script_name,
                            stdout=stdout[:200],
                            stderr=stderr[:200],
                        )
                        output = {
                            "check": script_name,
                            "status": "fail",
                            "detail": f"JSON parse error. stdout={stdout[:200]}",
                        }

                    status = output.get("status", "fail")
                    detail = output.get("detail", "")
                    check_name = output.get("check", script_name)

                    # NFS에 raw 결과 저장
                    raw_file = raw_dir / f"{script_name}.json"
                    raw_file.write_text(json.dumps(output, ensure_ascii=False, indent=2))

                    # DB 저장
                    async with _SessionLocal() as session:
                        await _save_check_result(
                            session, job_id, check_name, status, detail, output
                        )

                    log.info("script.done", script=script_name, status=status)

        finally:
            # 원격 임시 디렉토리 정리
            await conn.run(f"rm -rf {remote_tmp}", check=False)

    # ---- 검수 완료 → validate 트리거 ----
    async with _SessionLocal() as session:
        await _update_job(
            session, job_id,
            status="validating",
            result_path=str(raw_dir),
        )

    from workers.validate import validate_results  # 순환 import 방지
    validate_results.apply_async(args=[job_id], queue="q_validate")
    log.info("inspect.done", job_id=job_id)


async def _mark_error(job_id: str, message: str) -> None:
    async with _SessionLocal() as session:
        await _update_job(session, job_id, status="error", error_message=message[:2000])


# ---------------------------------------------------------------------------
# Celery Task
# ---------------------------------------------------------------------------

@app.task(
    bind=True,
    queue="q_inspect",
    acks_late=True,
    max_retries=3,
    default_retry_delay=60,
    soft_time_limit=7200,
    time_limit=7500,
    name="workers.inspect.inspect_server",
)
def inspect_server(
    self,
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
) -> dict:
    """
    검수 실행 태스크.

    Args:
        job_id: Job UUID (str)
        target_host: 검수 대상 서버 IP/hostname
        target_user: SSH 접속 유저
        product_profile: checks/profiles/ 아래 JSON 프로파일 이름
    """
    log.info("inspect.start", job_id=job_id, host=target_host, profile=product_profile)
    try:
        asyncio.run(_async_inspect(job_id, target_host, target_user, product_profile))
        return {"job_id": job_id, "result": "ok"}
    except asyncssh.DisconnectError as exc:
        asyncio.run(_mark_error(job_id, f"SSH disconnect: {exc}"))
        raise self.retry(exc=exc)
    except asyncssh.PermissionDenied as exc:
        # 인증 실패는 재시도 무의미
        asyncio.run(_mark_error(job_id, f"SSH auth failed: {exc}"))
        raise
    except FileNotFoundError as exc:
        asyncio.run(_mark_error(job_id, str(exc)))
        raise
    except Exception as exc:
        asyncio.run(_mark_error(job_id, str(exc)))
        raise self.retry(exc=exc)

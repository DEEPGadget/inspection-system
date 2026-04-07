"""
q_validate worker — Rule Validator 우선 판정, 경계값 시에만 Verify Agent 호출.

플로우:
  rule_validator.evaluate()
    ├─ pass          → job.status=cleanup → cleanup 트리거
    ├─ fail          → job.status=failed  → cleanup 트리거
    └─ agent_required
          ↓
        call_verify_agent()
          ├─ pass    → job.status=cleanup → cleanup 트리거
          └─ reject  → job.status=rejected → cleanup 트리거

cleanup은 판정 결과와 무관하게 항상 실행.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings
from workers.agent_gateway import call_verify_agent
from workers.app import app
from workers.notify import publish_job_status
from workers.rule_validator import evaluate as rule_evaluate

log = structlog.get_logger(__name__)


def _make_session() -> tuple:
    engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_local


# ---------------------------------------------------------------------------
# 프로파일 로더
# ---------------------------------------------------------------------------

_PROFILES_DIR = Path(__file__).parent.parent / "checks" / "profiles"


def _load_rules(product_profile: str) -> tuple[list[dict], int]:
    """
    프로파일 JSON에서 validation.rules와 warn_count_threshold 반환.
    파일 없거나 파싱 실패 시 빈 rules, 기본 threshold=3 반환.
    """
    profile_path = _PROFILES_DIR / f"{product_profile}.json"
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
        rules = data.get("validation", {}).get("rules", [])
        threshold = (
            data.get("validation", {}).get("agent_trigger", {}).get("warn_count_threshold", 3)
        )
        return rules, threshold
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning("validate.profile_load_failed", profile=product_profile, error=str(exc))
        return [], 3


# ---------------------------------------------------------------------------
# DB 헬퍼
# ---------------------------------------------------------------------------


async def _load_job_and_results(session: AsyncSession, job_id: str) -> tuple:
    from api.models import CheckResult, Job

    job_result = await session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    job = job_result.scalar_one_or_none()
    if job is None:
        raise ValueError(f"Job {job_id} not found")

    cr_result = await session.execute(
        select(CheckResult)
        .where(CheckResult.job_id == uuid.UUID(job_id))
        .order_by(CheckResult.created_at.asc())
    )
    check_results = cr_result.scalars().all()
    return job, list(check_results)


async def _update_job_status(
    session: AsyncSession,
    job_id: str,
    status: str,
    error_message: str | None = None,
) -> None:
    from api.models import Job

    result = await session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    job = result.scalar_one_or_none()
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    job.status = status
    job.updated_at = datetime.now(timezone.utc)
    if error_message:
        job.error_message = error_message[:2000]
    await session.commit()


# ---------------------------------------------------------------------------
# NFS 결과 저장
# ---------------------------------------------------------------------------


def _save_verdict_to_nfs(
    job_id: str,
    rv_result: dict,
    agent_verdict: dict | None,
    final_verdict: str,
) -> None:
    """
    claude_verdict.json (v2 포맷) 저장.

    {
        "job_id": str,
        "verdict": "pass" | "fail" | "rejected",
        "fail_items": [...],
        "warn_items": [...],
        "warn_count": int,
        "agent_verdict": {"verdict": str, "reason": str} | null,
        "validated_at": ISO8601
    }
    """
    nfs_job_dir = Path(settings.nfs_base_path) / "results" / job_id
    nfs_job_dir.mkdir(parents=True, exist_ok=True)
    verdict_file = nfs_job_dir / "claude_verdict.json"
    verdict_file.write_text(
        json.dumps(
            {
                "job_id": job_id,
                "verdict": final_verdict,
                "fail_items": rv_result.get("fail_items", []),
                "warn_items": rv_result.get("warn_items", []),
                "warn_count": rv_result.get("warn_count", 0),
                "agent_verdict": agent_verdict,
                "validated_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


# ---------------------------------------------------------------------------
# 핵심 async 로직
# ---------------------------------------------------------------------------


async def _async_validate(
    job_id: str,
    sudo_password: str | None,
) -> None:
    engine, SessionLocal = _make_session()
    try:
        # ── 1. DB 로드 ────────────────────────────────────────
        async with SessionLocal() as session:
            job, check_results = await _load_job_and_results(session, job_id)
            target_host = job.target_host
            target_user = job.target_user
            product_profile = job.product_profile
            expected_specs = job.expected_specs  # dict | None

        if not check_results:
            log.warning("validate.no_results", job_id=job_id)
            async with SessionLocal() as session:
                await _update_job_status(session, job_id, "failed", "검수 결과 없음")
            await publish_job_status(job_id, "failed")
            return

        # ── 2. 프로파일 규칙 로드 ─────────────────────────────
        rules, warn_threshold = _load_rules(product_profile)

        # ── 3. Rule Validator (토큰 0) ────────────────────────
        log.info(
            "validate.rule_validator",
            job_id=job_id,
            rule_count=len(rules),
            check_count=len(check_results),
        )
        rv_result = rule_evaluate(rules, check_results, expected_specs, warn_threshold)
        rv_verdict = rv_result["verdict"]  # "pass" | "fail" | "agent_required"

        log.info(
            "validate.rule_verdict",
            job_id=job_id,
            verdict=rv_verdict,
            fail_count=len(rv_result["fail_items"]),
            warn_count=rv_result["warn_count"],
        )

        # ── 4. 분기 ───────────────────────────────────────────
        agent_verdict: dict | None = None

        if rv_verdict == "pass":
            final_verdict = "pass"
            new_status = "cleanup"

        elif rv_verdict == "fail":
            final_verdict = "fail"
            new_status = "failed"
            fail_summary = "; ".join(
                f"{i['check']}.{i['metric']}={i['value']}({i['rule']})"
                for i in rv_result["fail_items"]
            )
            log.warning("validate.rule_fail", job_id=job_id, summary=fail_summary)

        else:  # agent_required
            log.info(
                "validate.agent_required",
                job_id=job_id,
                warn_count=rv_result["warn_count"],
            )
            agent_verdict = await call_verify_agent(
                rv_result["warn_items"], job_id, target_host, product_profile
            )
            if agent_verdict["verdict"] == "pass":
                final_verdict = "pass"
                new_status = "cleanup"
            else:
                final_verdict = "rejected"
                new_status = "rejected"

            log.info(
                "validate.agent_verdict",
                job_id=job_id,
                verdict=agent_verdict["verdict"],
                reason=agent_verdict.get("reason", "")[:200],
            )

        # ── 5. NFS 저장 ───────────────────────────────────────
        _save_verdict_to_nfs(job_id, rv_result, agent_verdict, final_verdict)

        # ── 6. Job 상태 업데이트 ──────────────────────────────
        error_msg = None
        if final_verdict == "fail":
            error_msg = "; ".join(
                f"{i['check']}.{i['metric']}={i['value']}" for i in rv_result["fail_items"]
            )
        elif final_verdict == "rejected":
            error_msg = agent_verdict.get("reason", "") if agent_verdict else None

        async with SessionLocal() as session:
            await _update_job_status(session, job_id, new_status, error_msg)
        await publish_job_status(job_id, new_status)

        # ── 7. cleanup 트리거 (항상) ──────────────────────────
        from workers.inspect import run_cleanup

        run_cleanup.apply_async(
            args=[job_id, target_host, target_user, product_profile],
            kwargs={"sudo_password": sudo_password},
            queue="q_inspect",
        )
        log.info("validate.cleanup_triggered", job_id=job_id, final_verdict=final_verdict)

    finally:
        await engine.dispose()


async def _mark_failed(job_id: str, message: str) -> None:
    engine, SessionLocal = _make_session()
    try:
        async with SessionLocal() as session:
            await _update_job_status(session, job_id, "failed", message[:2000])
        await publish_job_status(job_id, "failed")
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Celery Task
# ---------------------------------------------------------------------------


@app.task(
    bind=True,
    queue="q_validate",
    acks_late=True,
    max_retries=3,
    default_retry_delay=20,
    name="workers.validate.validate_results",
)
def validate_results(
    self,
    job_id: str,
    sudo_password: str | None = None,
    target_host: str | None = None,
    target_user: str | None = None,
    product_profile: str | None = None,
) -> dict:
    """
    Rule Validator → Verify Agent fallback 판정 태스크.

    Args:
        job_id: Job UUID (str)
        sudo_password: cleanup 단계 SSH sudo용
        target_host: (미사용, DB 로드) — 이전 버전 호환용
        target_user: (미사용, DB 로드) — 이전 버전 호환용
        product_profile: (미사용, DB 로드) — 이전 버전 호환용
    """
    import anthropic

    log.info("validate.start", job_id=job_id)
    try:
        asyncio.run(_async_validate(job_id, sudo_password))
        return {"job_id": job_id, "result": "ok"}
    except anthropic.AuthenticationError as exc:
        asyncio.run(_mark_failed(job_id, f"Claude API auth error: {exc}"))
        raise
    except anthropic.RateLimitError as exc:
        asyncio.run(_mark_failed(job_id, f"Claude API rate limit: {exc}"))
        raise self.retry(exc=exc, countdown=60)
    except Exception as exc:
        asyncio.run(_mark_failed(job_id, str(exc)))
        raise self.retry(exc=exc)

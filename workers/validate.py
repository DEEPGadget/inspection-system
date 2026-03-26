"""
q_validate worker — NFS의 검수 결과를 Claude API로 판독, DB 업데이트 후 report 트리거.
concurrency=2 (celeryconfig) — Claude API rate limit 대응.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config.settings import settings
from workers.app import app
from workers.notify import publish_job_status

log = structlog.get_logger(__name__)


def _make_session() -> tuple:
    """매 asyncio.run() 루프마다 새 엔진+세션팩토리를 생성."""
    engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_local

_PROMPT_PATH = Path(__file__).parent.parent / "config" / "prompts" / "validation_gpu_server.txt"


# ---------------------------------------------------------------------------
# Claude API 호출 (tenacity 재시도 — rate limit / 일시 오류 대응)
# ---------------------------------------------------------------------------


@retry(
    retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIStatusError)),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
)
async def _call_claude(client: anthropic.AsyncAnthropic, user_content: str) -> str:
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    msg = await client.messages.create(
        model=settings.claude_model,
        max_tokens=settings.claude_max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    return msg.content[0].text


# ---------------------------------------------------------------------------
# 프롬프트 구성
# ---------------------------------------------------------------------------


def _build_user_message(
    job_id: str,
    target_host: str,
    product_profile: str,
    check_results: list[dict],
) -> str:
    results_json = json.dumps(check_results, ensure_ascii=False, indent=2)
    return (
        f"## 검수 대상 서버\n"
        f"- Job ID: {job_id}\n"
        f"- Host: {target_host}\n"
        f"- Profile: {product_profile}\n\n"
        f"## 검수 결과\n"
        f"```json\n{results_json}\n```\n\n"
        f"위 결과를 판정 규칙에 따라 분석하여 JSON으로 응답하세요."
    )


# ---------------------------------------------------------------------------
# Claude 응답 파싱
# ---------------------------------------------------------------------------


def _parse_claude_response(text: str) -> dict:
    """JSON 블록 추출 후 파싱. 실패 시 error 구조 반환."""
    # 마크다운 코드블록 제거
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # JSON만 추출 시도
        start = stripped.find("{")
        end = stripped.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(stripped[start:end])
            except json.JSONDecodeError:
                pass

    log.warning("claude.parse_failed", response_preview=text[:300])
    return {
        "overall": "error",
        "fail_reasons": ["Claude 응답 파싱 실패"],
        "warn_reasons": [],
        "checks": [],
        "summary": f"Claude API 응답을 파싱할 수 없습니다: {text[:200]}",
    }


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
        select(CheckResult).where(CheckResult.job_id == uuid.UUID(job_id))
    )
    check_results = cr_result.scalars().all()
    return job, list(check_results)


async def _update_check_verdicts(
    session: AsyncSession,
    job_id: str,
    verdict_map: dict[str, tuple[str, str]],  # name → (verdict, reason)
) -> None:
    from api.models import CheckResult

    cr_result = await session.execute(
        select(CheckResult).where(CheckResult.job_id == uuid.UUID(job_id))
    )
    now = datetime.now(timezone.utc)
    for cr in cr_result.scalars().all():
        if cr.check_name in verdict_map:
            verdict, reason = verdict_map[cr.check_name]
            cr.claude_verdict = f"[{verdict.upper()}] {reason}"
            cr.validated_at = now
    await session.commit()


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
# 핵심 async 로직
# ---------------------------------------------------------------------------


async def _async_validate(job_id: str) -> None:
    engine, SessionLocal = _make_session()
    try:
        # ── 1. DB에서 Job + CheckResult 로드 ──────────────────
        async with SessionLocal() as session:
            job, check_results = await _load_job_and_results(session, job_id)
            target_host = job.target_host
            product_profile = job.product_profile

        if not check_results:
            log.warning("validate.no_results", job_id=job_id)
            async with SessionLocal() as session:
                await _update_job_status(session, job_id, "error", "검수 결과 없음")
            return

        # ── 2. 프롬프트 구성 ──────────────────────────────────
        results_payload = [
            {
                "check": cr.check_name,
                "status": cr.status,
                "detail": cr.detail,
                "raw": cr.raw_output,
            }
            for cr in check_results
        ]
        user_msg = _build_user_message(job_id, target_host, product_profile, results_payload)

        log.info("validate.claude_call", job_id=job_id, check_count=len(check_results))

        # ── 3. Claude API 호출 ────────────────────────────────
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        raw_response = await _call_claude(client, user_msg)

        log.debug("validate.claude_raw", job_id=job_id, preview=raw_response[:200])

        # ── 4. 응답 파싱 ──────────────────────────────────────
        parsed = _parse_claude_response(raw_response)
        overall = parsed.get("overall", "error")

        # ── 5. CheckResult에 verdict 기록 ─────────────────────
        verdict_map: dict[str, tuple[str, str]] = {}
        for ch in parsed.get("checks", []):
            name = ch.get("name", "")
            verdict = ch.get("verdict", "warn")
            reason = ch.get("reason", "")
            if name:
                verdict_map[name] = (verdict, reason)

        async with SessionLocal() as session:
            await _update_check_verdicts(session, job_id, verdict_map)

        # ── 6. NFS에 판독 결과 저장 ───────────────────────────
        nfs_job_dir = Path(settings.nfs_base_path) / "results" / job_id
        nfs_job_dir.mkdir(parents=True, exist_ok=True)
        verdict_file = nfs_job_dir / "claude_verdict.json"
        verdict_file.write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "overall": overall,
                    "fail_reasons": parsed.get("fail_reasons", []),
                    "warn_reasons": parsed.get("warn_reasons", []),
                    "summary": parsed.get("summary", ""),
                    "checks": parsed.get("checks", []),
                    "validated_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        # ── 7. Job 상태 업데이트 ──────────────────────────────
        if overall == "pass":
            new_status = "reporting"
            log.info("validate.pass", job_id=job_id)
        elif overall == "fail":
            new_status = "reporting"  # fail도 리포트 생성
            fail_reasons = "; ".join(parsed.get("fail_reasons", []))
            log.warning("validate.fail", job_id=job_id, reasons=fail_reasons)
        else:
            new_status = "error"
            log.error("validate.error", job_id=job_id, overall=overall)

        async with SessionLocal() as session:
            await _update_job_status(
                session,
                job_id,
                new_status,
                error_message=(
                    "; ".join(parsed.get("fail_reasons", [])) if overall == "fail" else None
                ),
            )
        await publish_job_status(job_id, new_status)

        # ── 8. pass/fail 모두 report 트리거 ──────────────────
        if overall in ("pass", "fail"):
            from workers.report import generate_report

            generate_report.apply_async(args=[job_id], queue="q_report")
            log.info("validate.report_triggered", job_id=job_id, overall=overall)
    finally:
        await engine.dispose()


async def _mark_error(job_id: str, message: str) -> None:
    engine, SessionLocal = _make_session()
    try:
        async with SessionLocal() as session:
            await _update_job_status(session, job_id, "error", message[:2000])
        await publish_job_status(job_id, "error", message[:2000])
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
    default_retry_delay=30,
    name="workers.validate.validate_results",
)
def validate_results(self, job_id: str) -> dict:
    """
    Claude API 판독 태스크.

    Args:
        job_id: Job UUID (str)
    """
    log.info("validate.start", job_id=job_id)
    try:
        asyncio.run(_async_validate(job_id))
        return {"job_id": job_id, "result": "ok"}
    except anthropic.AuthenticationError as exc:
        # API 키 오류 — 재시도 무의미
        asyncio.run(_mark_error(job_id, f"Claude API auth error: {exc}"))
        raise
    except anthropic.RateLimitError as exc:
        asyncio.run(_mark_error(job_id, f"Claude API rate limit: {exc}"))
        raise self.retry(exc=exc, countdown=60)
    except Exception as exc:
        asyncio.run(_mark_error(job_id, str(exc)))
        raise self.retry(exc=exc)

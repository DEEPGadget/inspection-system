"""
Agent Gateway — 에이전트 호출 판단 + compact input 구성.

호출 주체:
  - validate.py    → call_verify_agent (agent_zone 경계값 종합 판정)
  - inspect.py     → call_inspect_agent (스크립트 실패 진단)
  - sw_planner.py  → call_sw_planner_agent (SW 요구사항 구조화 + 설치계획 생성)
  - sw_install.py  → call_sw_planner_agent (설치 실패 후 재계획)

토큰 정책:
  - Verify Agent:      max_tokens=512
  - Inspect Agent:     max_tokens=1024
  - SW Planner Agent:  max_tokens=1024

반환 구조:
  call_verify_agent     → {"verdict": "pass" | "reject", "reason": str}
  call_inspect_agent    → {"action": "retry_script" | "fail", "reason": str}
  call_sw_planner_agent → {"plan": list[dict], "reason": str}
"""

from __future__ import annotations

import json
from pathlib import Path

import anthropic
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config.settings import settings

log = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "config" / "prompts"
_VERIFY_PROMPT_PATH = _PROMPTS_DIR / "verify_agent.txt"
_INSPECT_PROMPT_PATH = _PROMPTS_DIR / "inspect_agent.txt"
_SW_PLANNER_PROMPT_PATH = _PROMPTS_DIR / "sw_planner_agent.txt"


# ---------------------------------------------------------------------------
# 공통 — Claude API 호출 (tenacity 재시도)
# ---------------------------------------------------------------------------


@retry(
    retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIStatusError)),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
)
async def _call_claude(
    client: anthropic.AsyncAnthropic,
    system_prompt: str,
    user_content: str,
    max_tokens: int,
) -> str:
    msg = await client.messages.create(
        model=settings.claude_model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    return msg.content[0].text


# ---------------------------------------------------------------------------
# 공통 — JSON 응답 파싱
# ---------------------------------------------------------------------------


def _parse_json_response(text: str) -> dict | None:
    """마크다운 코드블록 제거 후 JSON 파싱. 실패 시 None 반환."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        result = json.loads(stripped)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                result = json.loads(stripped[start:end])
                return result if isinstance(result, dict) else None
            except json.JSONDecodeError:
                pass

    return None


# ---------------------------------------------------------------------------
# Verify Agent
# ---------------------------------------------------------------------------


async def call_verify_agent(
    warn_items: list[dict],
    job_id: str,
    target_host: str,
    product_profile: str,
) -> dict:
    """
    Verify Agent 호출. agent_zone 해당 항목을 종합 판단.

    Args:
        warn_items: rule_validator.evaluate() 반환의 warn_items 리스트
        job_id: Job UUID str
        target_host: 검수 대상 호스트
        product_profile: 프로파일 이름

    Returns:
        {"verdict": "pass" | "reject", "reason": str}
    """
    system_prompt = _VERIFY_PROMPT_PATH.read_text(encoding="utf-8")
    user_content = (
        f"## 검수 대상\n"
        f"- Job ID: {job_id}\n"
        f"- Host: {target_host}\n"
        f"- Profile: {product_profile}\n\n"
        f"## 경계값 항목 (agent_zone 해당)\n"
        f"```json\n{json.dumps(warn_items, ensure_ascii=False, indent=2)}\n```\n\n"
        f"위 항목들을 종합 판단하여 JSON으로 응답하세요."
    )

    log.info("agent_gateway.verify.call", job_id=job_id, warn_count=len(warn_items))
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        raw = await _call_claude(client, system_prompt, user_content, max_tokens=512)
    except Exception as exc:
        log.error("agent_gateway.verify.error", job_id=job_id, error=str(exc))
        return {"verdict": "reject", "reason": f"Verify Agent 호출 실패: {exc}"}

    log.debug("agent_gateway.verify.raw", job_id=job_id, preview=raw[:200])

    parsed = _parse_json_response(raw)
    if parsed is None:
        log.warning("agent_gateway.verify.parse_failed", job_id=job_id, preview=raw[:300])
        return {"verdict": "reject", "reason": f"Verify Agent 응답 파싱 실패: {raw[:200]}"}

    verdict = parsed.get("verdict", "reject")
    if verdict not in ("pass", "reject"):
        log.warning("agent_gateway.verify.invalid_verdict", job_id=job_id, verdict=verdict)
        verdict = "reject"

    reason = parsed.get("reason", "")
    log.info("agent_gateway.verify.result", job_id=job_id, verdict=verdict)
    return {"verdict": verdict, "reason": reason}


# ---------------------------------------------------------------------------
# Inspect Agent
# ---------------------------------------------------------------------------


async def call_inspect_agent(
    job_id: str,
    stage: str,
    script: str,
    exit_code: int,
    stderr: str,
) -> dict:
    """
    Inspect Agent 호출. 스크립트 실패 진단 + 수정 액션 판단.

    Args:
        job_id: Job UUID str
        stage: 파이프라인 단계 (preflight / post_install / cleanup)
        script: 실패한 스크립트 이름
        exit_code: 스크립트 exit code
        stderr: 스크립트 stderr 출력 (민감정보 마스킹 후)

    Returns:
        {"action": "retry_script" | "fail", "reason": str}
    """
    system_prompt = _INSPECT_PROMPT_PATH.read_text(encoding="utf-8")
    user_content = (
        f"## 실패 정보\n"
        f"- Job ID: {job_id}\n"
        f"- Stage: {stage}\n"
        f"- Script: {script}\n"
        f"- Exit Code: {exit_code}\n\n"
        f"## stderr\n"
        f"```\n{stderr[:2000]}\n```\n\n"
        f"위 실패를 분석하여 JSON으로 응답하세요."
    )

    log.info("agent_gateway.inspect.call", job_id=job_id, stage=stage, script=script)
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        raw = await _call_claude(client, system_prompt, user_content, max_tokens=1024)
    except Exception as exc:
        log.error("agent_gateway.inspect.error", job_id=job_id, error=str(exc))
        return {"action": "fail", "reason": f"Inspect Agent 호출 실패: {exc}"}

    log.debug("agent_gateway.inspect.raw", job_id=job_id, preview=raw[:200])

    parsed = _parse_json_response(raw)
    if parsed is None:
        log.warning("agent_gateway.inspect.parse_failed", job_id=job_id, preview=raw[:300])
        return {"action": "fail", "reason": f"Inspect Agent 응답 파싱 실패: {raw[:200]}"}

    action = parsed.get("action", "fail")
    if action not in ("retry_script", "fail"):
        log.warning("agent_gateway.inspect.invalid_action", job_id=job_id, action=action)
        action = "fail"

    reason = parsed.get("reason", "")
    log.info("agent_gateway.inspect.result", job_id=job_id, action=action)
    return {"action": action, "reason": reason}


# ---------------------------------------------------------------------------
# SW Planner Agent
# ---------------------------------------------------------------------------


async def call_sw_planner_agent(
    job_id: str,
    sw_requirements: str,
    failed_step: str | None = None,
) -> dict:
    """
    SW Planner Agent 호출.
    비정형 SW 요구사항 구조화, 호환성 판단, 설치계획 JSON 생성.

    Args:
        job_id: Job UUID str
        sw_requirements: sw_requirements.md 원문
        failed_step: 실패한 설치 단계 (재계획 시; 최초 계획 시 None)

    Returns:
        {"plan": list[dict], "reason": str}
        에러 시: {"plan": [], "reason": "SW Planner Agent 호출 실패: <error>"}
    """
    system_prompt = _SW_PLANNER_PROMPT_PATH.read_text(encoding="utf-8")

    failed_section = f"\n## 실패한 설치 단계\n```\n{failed_step}\n```\n" if failed_step else ""
    user_content = (
        f"## Job ID\n{job_id}\n\n"
        f"## SW 요구사항\n```markdown\n{sw_requirements}\n```\n"
        f"{failed_section}\n"
        f"위 요구사항을 분석하여 설치 계획 JSON으로 응답하세요."
    )

    log.info("agent_gateway.sw_planner.call", job_id=job_id, has_failed_step=bool(failed_step))
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        raw = await _call_claude(client, system_prompt, user_content, max_tokens=1024)
    except Exception as exc:
        log.error("agent_gateway.sw_planner.error", job_id=job_id, error=str(exc))
        return {"plan": [], "reason": f"SW Planner Agent 호출 실패: {exc}"}

    log.debug("agent_gateway.sw_planner.raw", job_id=job_id, preview=raw[:200])

    parsed = _parse_json_response(raw)
    if parsed is None:
        log.warning("agent_gateway.sw_planner.parse_failed", job_id=job_id, preview=raw[:300])
        return {"plan": [], "reason": f"SW Planner Agent 응답 파싱 실패: {raw[:200]}"}

    plan = parsed.get("plan", [])
    if not isinstance(plan, list):
        log.warning("agent_gateway.sw_planner.invalid_plan", job_id=job_id)
        plan = []

    reason = parsed.get("reason", "")
    log.info("agent_gateway.sw_planner.result", job_id=job_id, plan_count=len(plan))
    return {"plan": plan, "reason": reason}

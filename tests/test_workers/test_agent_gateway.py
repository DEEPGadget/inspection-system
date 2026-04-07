"""
Agent Gateway 유닛 테스트.
Claude API 호출은 모두 mock — 실제 API 요청 없음.
"""

import pytest
from unittest.mock import AsyncMock, patch

from workers.agent_gateway import (
    _parse_json_response,
    call_inspect_agent,
    call_sw_planner_agent,
    call_verify_agent,
)


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _warn_item(check: str, metric: str, value: str, rule: str, threshold) -> dict:
    return {"check": check, "metric": metric, "value": value, "rule": rule, "threshold": threshold}


# ---------------------------------------------------------------------------
# _parse_json_response
# ---------------------------------------------------------------------------


def test_parse_json_basic():
    result = _parse_json_response('{"verdict": "pass", "reason": "정상"}')
    assert result == {"verdict": "pass", "reason": "정상"}


def test_parse_json_markdown_codeblock():
    text = '```json\n{"action": "fail", "reason": "하드웨어 결함"}\n```'
    result = _parse_json_response(text)
    assert result == {"action": "fail", "reason": "하드웨어 결함"}


def test_parse_json_markdown_no_closing():
    """닫는 ``` 없는 코드블록도 파싱."""
    text = '```\n{"verdict": "reject", "reason": "경계값 초과"}'
    result = _parse_json_response(text)
    assert result is not None
    assert result["verdict"] == "reject"


def test_parse_json_embedded_in_text():
    """JSON 앞뒤에 텍스트가 있어도 추출."""
    text = '다음은 판정 결과입니다.\n{"verdict": "pass", "reason": "정상"}\n이상입니다.'
    result = _parse_json_response(text)
    assert result is not None
    assert result["verdict"] == "pass"


def test_parse_json_invalid_returns_none():
    result = _parse_json_response("이것은 JSON이 아닙니다.")
    assert result is None


def test_parse_json_empty_returns_none():
    result = _parse_json_response("")
    assert result is None


def test_parse_json_list_returns_none():
    """유효한 JSON이라도 dict가 아니면 None 반환."""
    result = _parse_json_response('[{"verdict": "pass"}]')
    assert result is None


def test_parse_json_string_returns_none():
    """JSON string은 dict가 아니므로 None 반환."""
    result = _parse_json_response('"pass"')
    assert result is None


# ---------------------------------------------------------------------------
# call_verify_agent
# ---------------------------------------------------------------------------

WARN_ITEMS = [
    _warn_item("sw_gpu_sw", "gpu_max_temp_c", "80", "agent_zone_above", 75),
]


async def test_verify_agent_pass():
    with patch("workers.agent_gateway._call_claude", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"verdict": "pass", "reason": "측정값이 정상 범위"}'
        result = await call_verify_agent(WARN_ITEMS, "job-1", "host-1", "gpu_server")
    assert result["verdict"] == "pass"
    assert result["reason"] == "측정값이 정상 범위"


async def test_verify_agent_reject():
    with patch("workers.agent_gateway._call_claude", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"verdict": "reject", "reason": "복합 경계값으로 불합격"}'
        result = await call_verify_agent(WARN_ITEMS, "job-1", "host-1", "gpu_server")
    assert result["verdict"] == "reject"


async def test_verify_agent_api_error_returns_reject():
    """API 호출 실패 시 reject로 안전하게 fallback."""
    with patch("workers.agent_gateway._call_claude", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = Exception("API 연결 실패")
        result = await call_verify_agent(WARN_ITEMS, "job-1", "host-1", "gpu_server")
    assert result["verdict"] == "reject"
    assert "Verify Agent 호출 실패" in result["reason"]


async def test_verify_agent_parse_failure_returns_reject():
    """응답 파싱 실패 시 reject로 안전하게 fallback."""
    with patch("workers.agent_gateway._call_claude", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "판정: 합격"  # JSON 아님
        result = await call_verify_agent(WARN_ITEMS, "job-1", "host-1", "gpu_server")
    assert result["verdict"] == "reject"
    assert "파싱 실패" in result["reason"]


async def test_verify_agent_invalid_verdict_normalized():
    """예상 밖 verdict 값은 reject로 정규화."""
    with patch("workers.agent_gateway._call_claude", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"verdict": "unknown", "reason": "알 수 없음"}'
        result = await call_verify_agent(WARN_ITEMS, "job-1", "host-1", "gpu_server")
    assert result["verdict"] == "reject"


async def test_verify_agent_compact_input_only_warn_items():
    """call_verify_agent는 warn_items만 전달, full check_results 아님."""
    with patch("workers.agent_gateway._call_claude", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"verdict": "pass", "reason": "정상"}'
        await call_verify_agent(WARN_ITEMS, "job-1", "host-1", "gpu_server")
    _, _, user_content = mock_call.call_args.args
    # warn_items의 metric이 user_content에 포함되어 있어야 함
    assert "gpu_max_temp_c" in user_content
    # 전체 raw 결과가 아닌 compact input
    assert "agent_zone_above" in user_content


# ---------------------------------------------------------------------------
# call_inspect_agent
# ---------------------------------------------------------------------------


async def test_inspect_agent_retry_script():
    with patch("workers.agent_gateway._call_claude", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"action": "retry_script", "reason": "dpkg lock 일시 잠금"}'
        result = await call_inspect_agent(
            "job-1", "preflight", "sw_gpu_hw", 1, "E: Could not get lock /var/lib/dpkg/lock"
        )
    assert result["action"] == "retry_script"
    assert result["reason"] == "dpkg lock 일시 잠금"


async def test_inspect_agent_fail():
    with patch("workers.agent_gateway._call_claude", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"action": "fail", "reason": "GPU 미감지 — 하드웨어 결함"}'
        result = await call_inspect_agent(
            "job-1", "post_install", "sw_gpu_sw", 1, "No GPU detected"
        )
    assert result["action"] == "fail"


async def test_inspect_agent_api_error_returns_fail():
    """API 호출 실패 시 fail로 안전하게 fallback."""
    with patch("workers.agent_gateway._call_claude", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = Exception("timeout")
        result = await call_inspect_agent("job-1", "preflight", "sw_cpu", 1, "")
    assert result["action"] == "fail"
    assert "Inspect Agent 호출 실패" in result["reason"]


async def test_inspect_agent_parse_failure_returns_fail():
    with patch("workers.agent_gateway._call_claude", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "재시도하세요"  # JSON 아님
        result = await call_inspect_agent("job-1", "preflight", "sw_cpu", 1, "")
    assert result["action"] == "fail"
    assert "파싱 실패" in result["reason"]


async def test_inspect_agent_invalid_action_normalized():
    """예상 밖 action 값은 fail로 정규화."""
    with patch("workers.agent_gateway._call_claude", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"action": "escalate", "reason": "모름"}'
        result = await call_inspect_agent("job-1", "preflight", "sw_cpu", 1, "")
    assert result["action"] == "fail"


async def test_inspect_agent_stderr_truncated_to_2000():
    """stderr가 2000자 초과 시 2000자로 truncate하여 전달."""
    long_stderr = "E: error\n" * 500  # 훨씬 길게
    with patch("workers.agent_gateway._call_claude", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"action": "fail", "reason": "오류"}'
        await call_inspect_agent("job-1", "preflight", "sw_cpu", 1, long_stderr)
    _, _, user_content = mock_call.call_args.args
    # stderr 섹션이 2000자 이내인지 확인 (전체 user_content 길이로 간접 검증)
    # 실제 truncate는 stderr[:2000] 으로 처리됨
    assert long_stderr[:2000] in user_content
    assert long_stderr[2001:] not in user_content


# ---------------------------------------------------------------------------
# call_sw_planner_agent (stub)
# ---------------------------------------------------------------------------


async def test_sw_planner_agent_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        await call_sw_planner_agent("job-1", "- CUDA 12.4\n- PyTorch 2.3")

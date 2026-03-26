"""
Validate Worker 유닛 테스트.
Claude API와 DB는 mock — 프롬프트 구성, 파싱, 상태 전이 로직 검증.
"""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.validate import _build_user_message, _parse_claude_response


@pytest.fixture(autouse=True)
def mock_publish(monkeypatch):
    """publish_job_status는 Redis 연결이 필요 — 모든 테스트에서 mock."""
    monkeypatch.setattr("workers.validate.publish_job_status", AsyncMock())


# ---------------------------------------------------------------------------
# 프롬프트 구성
# ---------------------------------------------------------------------------

def test_build_user_message_contains_host():
    msg = _build_user_message("job-1", "10.0.0.1", "gpu_server", [])
    assert "10.0.0.1" in msg
    assert "gpu_server" in msg


def test_build_user_message_contains_results():
    results = [{"check": "sw_gpu", "status": "pass", "detail": "8x A100", "raw": {}}]
    msg = _build_user_message("job-1", "10.0.0.1", "gpu_server", results)
    assert "sw_gpu" in msg
    assert "8x A100" in msg


# ---------------------------------------------------------------------------
# Claude 응답 파싱
# ---------------------------------------------------------------------------

VALID_RESPONSE = json.dumps({
    "overall": "pass",
    "fail_reasons": [],
    "warn_reasons": [],
    "checks": [
        {"name": "sw_gpu", "verdict": "pass", "reason": "8x A100, 45°C, no ECC errors"},
        {"name": "sw_power_mgmt", "verdict": "pass", "reason": "sleep.target masked, governor=performance"},
    ],
    "summary": "모든 검사를 통과했습니다.",
})

FAIL_RESPONSE = json.dumps({
    "overall": "fail",
    "fail_reasons": ["GPU 온도 92°C > 87°C", "sleep.target not masked"],
    "warn_reasons": [],
    "checks": [
        {"name": "sw_gpu", "verdict": "fail", "reason": "온도 92°C 초과"},
        {"name": "sw_power_mgmt", "verdict": "fail", "reason": "sleep.target masked 아님"},
    ],
    "summary": "GPU 과열 및 전원 관리 설정 불량으로 불합격.",
})


def test_parse_valid_json():
    result = _parse_claude_response(VALID_RESPONSE)
    assert result["overall"] == "pass"
    assert len(result["checks"]) == 2
    assert result["checks"][0]["name"] == "sw_gpu"


def test_parse_fail_response():
    result = _parse_claude_response(FAIL_RESPONSE)
    assert result["overall"] == "fail"
    assert len(result["fail_reasons"]) == 2


def test_parse_markdown_codeblock():
    wrapped = f"```json\n{VALID_RESPONSE}\n```"
    result = _parse_claude_response(wrapped)
    assert result["overall"] == "pass"


def test_parse_markdown_codeblock_no_lang():
    wrapped = f"```\n{VALID_RESPONSE}\n```"
    result = _parse_claude_response(wrapped)
    assert result["overall"] == "pass"


def test_parse_json_embedded_in_text():
    """JSON 앞뒤에 텍스트가 있는 경우 추출."""
    text = f"판정 결과입니다:\n{VALID_RESPONSE}\n이상입니다."
    result = _parse_claude_response(text)
    assert result["overall"] == "pass"


def test_parse_invalid_returns_error():
    result = _parse_claude_response("이것은 JSON이 아닙니다.")
    assert result["overall"] == "error"
    assert "fail_reasons" in result
    assert len(result["fail_reasons"]) > 0


def test_parse_empty_string():
    result = _parse_claude_response("")
    assert result["overall"] == "error"


# ---------------------------------------------------------------------------
# 상태 전이 로직 (전체 흐름 mock)
# ---------------------------------------------------------------------------

MOCK_CHECK_RESULTS = [
    MagicMock(
        check_name="sw_gpu",
        status="pass",
        detail="gpu_count=8|gpu_max_temp_c=45",
        raw_output={"check": "sw_gpu", "status": "pass", "detail": "gpu_count=8"},
    ),
    MagicMock(
        check_name="sw_power_mgmt",
        status="fail",
        detail="FAIL:sleep_target_not_masked",
        raw_output={"check": "sw_power_mgmt", "status": "fail", "detail": "sleep.target=enabled"},
    ),
]


@pytest.mark.asyncio
async def test_async_validate_pass_triggers_report(tmp_path, monkeypatch):
    """overall=pass 시 generate_report.apply_async가 호출되는지 확인."""
    job_id = str(uuid.uuid4())
    monkeypatch.setattr("workers.validate.settings.nfs_base_path", str(tmp_path))
    monkeypatch.setattr("workers.validate.settings.anthropic_api_key", "test-key")

    # DB mock
    mock_job = MagicMock(
        target_host="10.0.0.1",
        product_profile="gpu_server",
    )
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=mock_job),
        scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=MOCK_CHECK_RESULTS))),
    ))
    mock_session.commit = AsyncMock()

    mock_report = MagicMock()
    mock_report.apply_async = MagicMock()

    with (
        patch("workers.validate._SessionLocal", MagicMock(return_value=mock_session)),
        patch("workers.validate._load_job_and_results",
              AsyncMock(return_value=(mock_job, MOCK_CHECK_RESULTS))),
        patch("workers.validate._update_check_verdicts", AsyncMock()),
        patch("workers.validate._update_job_status", AsyncMock()),
        patch("workers.validate._call_claude", AsyncMock(return_value=VALID_RESPONSE)),
        patch.dict("sys.modules", {"workers.report": MagicMock(generate_report=mock_report)}),
    ):
        from workers.validate import _async_validate
        await _async_validate(job_id)

    # NFS verdict 파일 생성 확인
    verdict_file = tmp_path / "results" / job_id / "claude_verdict.json"
    assert verdict_file.exists()
    data = json.loads(verdict_file.read_text())
    assert data["overall"] == "pass"


@pytest.mark.asyncio
async def test_async_validate_fail_no_report(tmp_path, monkeypatch):
    """overall=fail 시 report가 트리거되지 않는지 확인."""
    job_id = str(uuid.uuid4())
    monkeypatch.setattr("workers.validate.settings.nfs_base_path", str(tmp_path))
    monkeypatch.setattr("workers.validate.settings.anthropic_api_key", "test-key")

    mock_job = MagicMock(target_host="10.0.0.1", product_profile="gpu_server")
    mock_report = MagicMock()
    mock_report.apply_async = MagicMock()

    with (
        patch("workers.validate._load_job_and_results",
              AsyncMock(return_value=(mock_job, MOCK_CHECK_RESULTS))),
        patch("workers.validate._update_check_verdicts", AsyncMock()),
        patch("workers.validate._update_job_status", AsyncMock()),
        patch("workers.validate._call_claude", AsyncMock(return_value=FAIL_RESPONSE)),
        patch.dict("sys.modules", {"workers.report": MagicMock(generate_report=mock_report)}),
    ):
        from workers.validate import _async_validate
        await _async_validate(job_id)

    mock_report.apply_async.assert_not_called()


@pytest.mark.asyncio
async def test_async_validate_no_results_marks_error(monkeypatch):
    """CheckResult 없으면 Job을 error로 마킹."""
    job_id = str(uuid.uuid4())
    mock_job = MagicMock(target_host="10.0.0.1", product_profile="gpu_server")

    update_status = AsyncMock()
    with (
        patch("workers.validate._load_job_and_results",
              AsyncMock(return_value=(mock_job, []))),  # 빈 결과
        patch("workers.validate._update_job_status", update_status),
    ):
        from workers.validate import _async_validate
        await _async_validate(job_id)

    update_status.assert_called_once()
    args = update_status.call_args
    assert args[0][2] == "error"  # status 인자

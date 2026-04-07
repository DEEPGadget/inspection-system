"""
Validate Worker v2 유닛 테스트.
DB·Claude API·NFS는 mock — Rule Validator 연동, Verify Agent fallback, 상태 전이 검증.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.validate import _load_rules, _save_verdict_to_nfs


# ---------------------------------------------------------------------------
# 공통 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_publish(monkeypatch):
    """publish_job_status — Redis 불필요."""
    monkeypatch.setattr("workers.validate.publish_job_status", AsyncMock())


@pytest.fixture
def mock_session(monkeypatch):
    """_make_session → 가짜 엔진 + 세션팩토리."""
    sess = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    engine = AsyncMock()
    monkeypatch.setattr("workers.validate._make_session", lambda: (engine, factory))
    return sess


def _mock_job(profile="gpu_server", expected_specs=None):
    return MagicMock(
        target_host="10.0.0.1",
        target_user="root",
        product_profile=profile,
        expected_specs=expected_specs,
    )


def _mock_cr(check_name: str, detail: str, status: str = "pass"):
    """최소 CheckResult 목."""
    cr = MagicMock()
    cr.check_name = check_name
    cr.detail = detail
    cr.status = status
    cr.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return cr


MOCK_CRS = [
    _mock_cr("sw_gpu_sw", "gpu_max_temp_c=80"),
    _mock_cr("sw_power_mgmt", "sleep_target=masked"),
]


# ---------------------------------------------------------------------------
# _load_rules
# ---------------------------------------------------------------------------


def test_load_rules_valid(tmp_path, monkeypatch):
    monkeypatch.setattr("workers.validate._PROFILES_DIR", tmp_path)
    profile_data = {
        "validation": {
            "rules": [{"check": "sw_gpu_sw", "metric": "gpu_max_temp_c", "fail_above": 87}],
            "agent_trigger": {"warn_count_threshold": 2},
        }
    }
    (tmp_path / "gpu_server.json").write_text(json.dumps(profile_data))
    rules, threshold = _load_rules("gpu_server")
    assert len(rules) == 1
    assert threshold == 2


def test_load_rules_file_not_found(tmp_path, monkeypatch):
    """프로파일 파일 없으면 빈 rules, 기본 threshold=3 반환."""
    monkeypatch.setattr("workers.validate._PROFILES_DIR", tmp_path)
    rules, threshold = _load_rules("nonexistent")
    assert rules == []
    assert threshold == 3


def test_load_rules_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setattr("workers.validate._PROFILES_DIR", tmp_path)
    (tmp_path / "bad.json").write_text("not json")
    rules, threshold = _load_rules("bad")
    assert rules == []
    assert threshold == 3


# ---------------------------------------------------------------------------
# _save_verdict_to_nfs
# ---------------------------------------------------------------------------


def test_save_verdict_creates_file(tmp_path, monkeypatch):
    monkeypatch.setattr("workers.validate.settings.nfs_base_path", str(tmp_path))
    job_id = str(uuid.uuid4())
    rv_result = {"fail_items": [], "warn_items": [], "warn_count": 0}
    _save_verdict_to_nfs(job_id, rv_result, None, "pass")

    verdict_file = tmp_path / "results" / job_id / "claude_verdict.json"
    assert verdict_file.exists()
    data = json.loads(verdict_file.read_text())
    assert data["verdict"] == "pass"
    assert data["agent_verdict"] is None
    assert "validated_at" in data


def test_save_verdict_with_agent_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr("workers.validate.settings.nfs_base_path", str(tmp_path))
    job_id = str(uuid.uuid4())
    rv_result = {
        "fail_items": [],
        "warn_items": [{"check": "sw_gpu_sw", "metric": "gpu_max_temp_c"}],
        "warn_count": 1,
    }
    agent_v = {"verdict": "reject", "reason": "복합 경계값으로 불합격"}
    _save_verdict_to_nfs(job_id, rv_result, agent_v, "rejected")

    data = json.loads((tmp_path / "results" / job_id / "claude_verdict.json").read_text())
    assert data["verdict"] == "rejected"
    assert data["agent_verdict"]["verdict"] == "reject"
    assert data["warn_count"] == 1


# ---------------------------------------------------------------------------
# _async_validate — pass 플로우
# ---------------------------------------------------------------------------


async def test_validate_rule_pass_triggers_cleanup(tmp_path, monkeypatch, mock_session):
    """rule_validator=pass → job.status=cleanup, cleanup 트리거."""
    monkeypatch.setattr("workers.validate.settings.nfs_base_path", str(tmp_path))
    job_id = str(uuid.uuid4())

    mock_cleanup = MagicMock()
    mock_cleanup.apply_async = MagicMock()
    mock_update = AsyncMock()

    with (
        patch(
            "workers.validate._load_job_and_results",
            AsyncMock(return_value=(_mock_job(), MOCK_CRS)),
        ),
        patch("workers.validate._update_job_status", mock_update),
        patch(
            "workers.validate.rule_evaluate",
            return_value={"verdict": "pass", "fail_items": [], "warn_items": [], "warn_count": 0},
        ),
        patch.dict("sys.modules", {"workers.inspect": MagicMock(run_cleanup=mock_cleanup)}),
    ):
        from workers.validate import _async_validate

        await _async_validate(job_id, sudo_password=None)

    # status = cleanup
    mock_update.assert_called_once()
    assert mock_update.call_args[0][2] == "cleanup"
    # NFS 파일 생성
    verdict_file = tmp_path / "results" / job_id / "claude_verdict.json"
    assert verdict_file.exists()
    assert json.loads(verdict_file.read_text())["verdict"] == "pass"
    # cleanup 트리거
    mock_cleanup.apply_async.assert_called_once()


# ---------------------------------------------------------------------------
# _async_validate — fail 플로우
# ---------------------------------------------------------------------------


async def test_validate_rule_fail_triggers_cleanup(tmp_path, monkeypatch, mock_session):
    """rule_validator=fail → job.status=failed, cleanup 트리거."""
    monkeypatch.setattr("workers.validate.settings.nfs_base_path", str(tmp_path))
    job_id = str(uuid.uuid4())

    mock_cleanup = MagicMock()
    mock_cleanup.apply_async = MagicMock()
    mock_update = AsyncMock()

    fail_items = [
        {
            "check": "sw_gpu_sw",
            "metric": "gpu_max_temp_c",
            "value": "92",
            "rule": "fail_above",
            "threshold": 87,
        }
    ]
    with (
        patch(
            "workers.validate._load_job_and_results",
            AsyncMock(return_value=(_mock_job(), MOCK_CRS)),
        ),
        patch("workers.validate._update_job_status", mock_update),
        patch(
            "workers.validate.rule_evaluate",
            return_value={
                "verdict": "fail",
                "fail_items": fail_items,
                "warn_items": [],
                "warn_count": 0,
            },
        ),
        patch.dict("sys.modules", {"workers.inspect": MagicMock(run_cleanup=mock_cleanup)}),
    ):
        from workers.validate import _async_validate

        await _async_validate(job_id, sudo_password=None)

    assert mock_update.call_args[0][2] == "failed"
    mock_cleanup.apply_async.assert_called_once()
    data = json.loads((tmp_path / "results" / job_id / "claude_verdict.json").read_text())
    assert data["verdict"] == "fail"
    assert len(data["fail_items"]) == 1


# ---------------------------------------------------------------------------
# _async_validate — agent_required → pass
# ---------------------------------------------------------------------------


async def test_validate_agent_required_pass(tmp_path, monkeypatch, mock_session):
    """agent_required + Verify Agent pass → job.status=cleanup."""
    monkeypatch.setattr("workers.validate.settings.nfs_base_path", str(tmp_path))
    job_id = str(uuid.uuid4())

    mock_cleanup = MagicMock()
    mock_cleanup.apply_async = MagicMock()
    mock_update = AsyncMock()
    warn_items = [
        {
            "check": "sw_gpu_sw",
            "metric": "gpu_max_temp_c",
            "value": "80",
            "rule": "agent_zone_above",
            "threshold": 75,
        }
    ]

    with (
        patch(
            "workers.validate._load_job_and_results",
            AsyncMock(return_value=(_mock_job(), MOCK_CRS)),
        ),
        patch("workers.validate._update_job_status", mock_update),
        patch(
            "workers.validate.rule_evaluate",
            return_value={
                "verdict": "agent_required",
                "fail_items": [],
                "warn_items": warn_items,
                "warn_count": 1,
            },
        ),
        patch(
            "workers.validate.call_verify_agent",
            AsyncMock(return_value={"verdict": "pass", "reason": "경계값이나 종합 정상"}),
        ),
        patch.dict("sys.modules", {"workers.inspect": MagicMock(run_cleanup=mock_cleanup)}),
    ):
        from workers.validate import _async_validate

        await _async_validate(job_id, sudo_password=None)

    assert mock_update.call_args[0][2] == "cleanup"
    mock_cleanup.apply_async.assert_called_once()
    data = json.loads((tmp_path / "results" / job_id / "claude_verdict.json").read_text())
    assert data["verdict"] == "pass"
    assert data["agent_verdict"]["verdict"] == "pass"


# ---------------------------------------------------------------------------
# _async_validate — agent_required → reject
# ---------------------------------------------------------------------------


async def test_validate_agent_required_reject(tmp_path, monkeypatch, mock_session):
    """agent_required + Verify Agent reject → job.status=rejected, cleanup 트리거."""
    monkeypatch.setattr("workers.validate.settings.nfs_base_path", str(tmp_path))
    job_id = str(uuid.uuid4())

    mock_cleanup = MagicMock()
    mock_cleanup.apply_async = MagicMock()
    mock_update = AsyncMock()
    warn_items = [
        {
            "check": "sw_gpu_sw",
            "metric": "gpu_max_temp_c",
            "value": "80",
            "rule": "agent_zone_above",
            "threshold": 75,
        }
    ]

    with (
        patch(
            "workers.validate._load_job_and_results",
            AsyncMock(return_value=(_mock_job(), MOCK_CRS)),
        ),
        patch("workers.validate._update_job_status", mock_update),
        patch(
            "workers.validate.rule_evaluate",
            return_value={
                "verdict": "agent_required",
                "fail_items": [],
                "warn_items": warn_items,
                "warn_count": 1,
            },
        ),
        patch(
            "workers.validate.call_verify_agent",
            AsyncMock(return_value={"verdict": "reject", "reason": "복합 경계값으로 불합격"}),
        ),
        patch.dict("sys.modules", {"workers.inspect": MagicMock(run_cleanup=mock_cleanup)}),
    ):
        from workers.validate import _async_validate

        await _async_validate(job_id, sudo_password=None)

    assert mock_update.call_args[0][2] == "rejected"
    # cleanup은 rejected여도 실행
    mock_cleanup.apply_async.assert_called_once()
    data = json.loads((tmp_path / "results" / job_id / "claude_verdict.json").read_text())
    assert data["verdict"] == "rejected"
    assert data["agent_verdict"]["verdict"] == "reject"


# ---------------------------------------------------------------------------
# _async_validate — edge cases
# ---------------------------------------------------------------------------


async def test_validate_no_results_marks_failed(monkeypatch, mock_session):
    """CheckResult 없으면 job.status=failed."""
    job_id = str(uuid.uuid4())
    mock_update = AsyncMock()

    with (
        patch(
            "workers.validate._load_job_and_results",
            AsyncMock(return_value=(_mock_job(), [])),
        ),
        patch("workers.validate._update_job_status", mock_update),
    ):
        from workers.validate import _async_validate

        await _async_validate(job_id, sudo_password=None)

    assert mock_update.call_args[0][2] == "failed"


async def test_validate_expected_specs_passed_to_rule_evaluate(tmp_path, monkeypatch, mock_session):
    """job.expected_specs가 rule_evaluate에 전달되는지 확인."""
    monkeypatch.setattr("workers.validate.settings.nfs_base_path", str(tmp_path))
    job_id = str(uuid.uuid4())
    mock_cleanup = MagicMock()
    mock_cleanup.apply_async = MagicMock()

    mock_evaluate = MagicMock(
        return_value={"verdict": "pass", "fail_items": [], "warn_items": [], "warn_count": 0}
    )
    expected = {"expected_gpu_count": 8}

    with (
        patch(
            "workers.validate._load_job_and_results",
            AsyncMock(return_value=(_mock_job(expected_specs=expected), MOCK_CRS)),
        ),
        patch("workers.validate._update_job_status", AsyncMock()),
        patch("workers.validate.rule_evaluate", mock_evaluate),
        patch.dict("sys.modules", {"workers.inspect": MagicMock(run_cleanup=mock_cleanup)}),
    ):
        from workers.validate import _async_validate

        await _async_validate(job_id, sudo_password=None)

    # rule_evaluate 두 번째 인자가 check_results, 세 번째가 expected_specs
    call_kwargs = mock_evaluate.call_args
    assert call_kwargs[0][2] == expected

"""
config/logging.py — mask_sensitive_fields 프로세서 및 configure_logging() 테스트.
"""

import pytest

from config.logging import configure_logging, mask_sensitive_fields


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────


def _process(event_dict: dict) -> dict:
    """mask_sensitive_fields 프로세서를 직접 호출."""
    return mask_sensitive_fields(None, "info", event_dict)


# ── 키 마스킹 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    ["password", "sudo_password", "api_key", "anthropic_api_key", "token", "secret"],
)
def test_masked_key_lowercase(key):
    """소문자 민감 키 → '***'."""
    result = _process({"event": "test", key: "supersecret"})
    assert result[key] == "***"


@pytest.mark.parametrize(
    "key",
    ["PASSWORD", "SUDO_PASSWORD", "API_KEY", "ANTHROPIC_API_KEY", "TOKEN", "SECRET"],
)
def test_masked_key_uppercase(key):
    """대문자 민감 키 → '***'."""
    result = _process({"event": "test", key: "supersecret"})
    assert result[key] == "***"


def test_masked_key_mixed_case():
    """혼합 대소문자 키 → '***'."""
    result = _process({"event": "test", "Password": "mysecret", "Api_Key": "mykey"})
    assert result["Password"] == "***"
    assert result["Api_Key"] == "***"


# ── 안전 키 유지 ──────────────────────────────────────────────────────────────


def test_non_sensitive_keys_untouched():
    """민감하지 않은 키는 그대로 유지."""
    result = _process(
        {
            "event": "ssh connected",
            "job_id": "abc-123",
            "target_host": "10.100.1.50",
            "target_user": "ubuntu",
            "stage": "sw_install",
        }
    )
    assert result["target_host"] == "10.100.1.50"
    assert result["target_user"] == "ubuntu"
    assert result["job_id"] == "abc-123"
    assert result["stage"] == "sw_install"


def test_event_key_untouched():
    """'event' 키(메시지 본문)는 마스킹 대상이 아님."""
    result = _process({"event": "install complete", "status": "ok"})
    assert result["event"] == "install complete"


# ── 중첩 dict 마스킹 ──────────────────────────────────────────────────────────


def test_nested_dict_masked():
    """중첩 dict 안의 민감 키도 마스킹."""
    result = _process(
        {
            "event": "job created",
            "credentials": {"password": "p@ssw0rd", "user": "ubuntu"},
        }
    )
    assert result["credentials"]["password"] == "***"
    assert result["credentials"]["user"] == "ubuntu"


def test_deeply_nested_dict_masked():
    """2단계 이상 중첩 dict 마스킹."""
    result = _process({"outer": {"inner": {"sudo_password": "secret123"}}})
    assert result["outer"]["inner"]["sudo_password"] == "***"


# ── 문자열 값 내 패턴 마스킹 (traceback 등) ───────────────────────────────────


@pytest.mark.parametrize(
    "original, expected_fragment",
    [
        ("PASSWORD=mysecret", "PASSWORD=***"),
        ("SECRET=abc123", "SECRET=***"),
        ("TOKEN=bearer_xyz", "TOKEN=***"),
        ("API_KEY=sk-1234", "API_KEY=***"),
        ("ANTHROPIC_API_KEY=sk-ant-abc", "ANTHROPIC_API_KEY=***"),
        ("sudo_password=p@ss", "sudo_password=***"),
    ],
)
def test_value_pattern_in_string(original, expected_fragment):
    """문자열 값 내 KEY=value 패턴 → KEY=*** 치환."""
    result = _process({"event": "test", "detail": original})
    assert expected_fragment in result["detail"]


def test_value_pattern_multiple_in_one_string():
    """한 문자열 안에 여러 패턴이 있을 때 모두 치환."""
    s = "env: PASSWORD=foo TOKEN=bar HOST=10.0.0.1"
    result = _process({"event": "traceback", "exception": s})
    assert "PASSWORD=***" in result["exception"]
    assert "TOKEN=***" in result["exception"]
    assert "HOST=10.0.0.1" in result["exception"]  # 비민감 유지


def test_host_in_string_untouched():
    """문자열 내 HOST, IP 등 비민감 패턴은 유지."""
    result = _process({"event": "test", "msg": "connecting to 10.100.1.50 port 22"})
    assert "10.100.1.50" in result["msg"]


# ── 리스트 내 마스킹 ──────────────────────────────────────────────────────────


def test_list_of_strings_masked():
    """리스트 내 문자열도 패턴 치환."""
    result = _process({"event": "test", "lines": ["PASSWORD=foo", "HOST=server1"]})
    assert result["lines"][0] == "PASSWORD=***"
    assert result["lines"][1] == "HOST=server1"


def test_list_of_dicts_masked():
    """리스트 내 dict의 민감 키도 마스킹."""
    result = _process({"event": "test", "items": [{"password": "x"}, {"user": "ubuntu"}]})
    assert result["items"][0]["password"] == "***"
    assert result["items"][1]["user"] == "ubuntu"


# ── configure_logging() ───────────────────────────────────────────────────────


def test_configure_logging_runs_without_error():
    """configure_logging() 호출이 예외 없이 완료."""
    configure_logging()


def test_configure_logging_idempotent():
    """configure_logging() 여러 번 호출해도 예외 없음."""
    configure_logging()
    configure_logging()

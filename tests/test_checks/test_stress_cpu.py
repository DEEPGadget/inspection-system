"""
stress_cpu.py 유닛 테스트.
stress-ng 있는 환경에서만 직접 실행 테스트 수행 (python3 fallback은 고코어 서버에서 느림).
mock 출력 파싱 테스트는 항상 실행.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent.parent / "checks" / "base" / "post_install" / "stress_cpu.py"

_HAS_STRESS_NG = subprocess.run(["which", "stress-ng"], capture_output=True).returncode == 0


def _run(duration: str = "3", timeout: int = 30) -> dict:
    env = {**os.environ, "CPU_BURNIN_DURATION": duration}
    result = subprocess.run(
        ["python3", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    stdout = result.stdout.strip()
    last_line = stdout.splitlines()[-1] if stdout else ""
    return json.loads(last_line)


# ---------------------------------------------------------------------------
# 직접 실행 (stress-ng 있는 환경만)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_STRESS_NG, reason="stress-ng 미설치")
def test_output_has_required_keys():
    data = _run()
    assert "check" in data
    assert "status" in data
    assert "detail" in data


@pytest.mark.skipif(not _HAS_STRESS_NG, reason="stress-ng 미설치")
def test_check_name():
    data = _run()
    assert data["check"] == "stress_cpu"


@pytest.mark.skipif(not _HAS_STRESS_NG, reason="stress-ng 미설치")
def test_status_is_valid():
    data = _run()
    assert data["status"] in ("pass", "fail", "warn")


@pytest.mark.skipif(not _HAS_STRESS_NG, reason="stress-ng 미설치")
def test_detail_contains_required_metrics():
    data = _run()
    detail = data["detail"]
    for field in ("logical_cpus", "duration_s", "tool", "peak_temp_c", "avg_util_pct"):
        assert field in detail, f"missing field: {field}"


# ---------------------------------------------------------------------------
# mock 출력 파싱 (항상 실행)
# ---------------------------------------------------------------------------


def test_mock_output_pass():
    sample = (
        '{"check":"stress_cpu","status":"pass",'
        '"detail":"logical_cpus=128|duration_s=120|tool=stress-ng'
        "|peak_temp_c=72|max_freq_mhz=3600|min_freq_mhz_under_load=3550"
        '|avg_util_pct=99|throttle_sample_count=0"}'
    )
    data = json.loads(sample)
    assert data["status"] == "pass"


def test_mock_output_fail_overtemp():
    sample = (
        '{"check":"stress_cpu","status":"fail",'
        '"detail":"logical_cpus=64|duration_s=120|tool=stress-ng'
        '|peak_temp_c=103|FAIL:peak_temp_over_100c(103c)"}'
    )
    data = json.loads(sample)
    assert data["status"] == "fail"
    assert "FAIL:peak_temp_over_100c" in data["detail"]


def test_mock_output_warn_throttle():
    sample = (
        '{"check":"stress_cpu","status":"warn",'
        '"detail":"logical_cpus=64|duration_s=120|tool=stress-ng'
        "|peak_temp_c=85|avg_util_pct=98|throttle_sample_count=3"
        '|WARN:freq_throttle_detected_3_samples"}'
    )
    data = json.loads(sample)
    assert data["status"] == "warn"
    assert "WARN:freq_throttle_detected_3_samples" in data["detail"]

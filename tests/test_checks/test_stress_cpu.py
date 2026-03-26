"""
stress_cpu.sh 유닛 테스트.
CI 환경에서 짧은 duration으로 실행하여 JSON 출력 규격과 메트릭 포함 여부 검증.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent.parent / "checks" / "base" / "phase4_stress" / "stress_cpu.sh"


def _run(duration: str = "3", timeout: int = 15) -> dict:
    env = {**os.environ, "CPU_BURNIN_DURATION": duration}
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    stdout = result.stdout.strip()
    last_line = stdout.splitlines()[-1] if stdout else ""
    return json.loads(last_line)


# ---------------------------------------------------------------------------
# 기본 출력 규격
# ---------------------------------------------------------------------------

def test_output_has_required_keys():
    """check / status / detail 필드 존재."""
    data = _run()
    assert "check" in data
    assert "status" in data
    assert "detail" in data


def test_check_name():
    data = _run()
    assert data["check"] == "stress_cpu"


def test_status_is_valid():
    data = _run()
    assert data["status"] in ("pass", "fail", "warn")


def test_detail_contains_required_metrics():
    """detail 문자열에 핵심 메트릭이 포함되는지."""
    data = _run()
    detail = data["detail"]
    for field in ("logical_cpus", "duration_s", "tool", "peak_temp_c", "avg_util_pct"):
        assert field in detail, f"missing field: {field}"


def test_detail_contains_logical_cpus():
    """logical_cpus 값이 양수인지."""
    data = _run()
    detail = data["detail"]
    # "logical_cpus=N" 형태에서 N 추출
    for part in detail.split("|"):
        if part.startswith("logical_cpus="):
            n = int(part.split("=", 1)[1])
            assert n >= 1
            break
    else:
        pytest.fail("logical_cpus not found in detail")


# ---------------------------------------------------------------------------
# shellcheck
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    subprocess.run(["which", "shellcheck"], capture_output=True).returncode != 0,
    reason="shellcheck 미설치",
)
def test_shellcheck_stress_cpu():
    result = subprocess.run(
        ["shellcheck", "-S", "warning", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"shellcheck 실패:\n{result.stdout}"


# ---------------------------------------------------------------------------
# mock 출력 파싱
# ---------------------------------------------------------------------------

def test_mock_output_pass():
    sample = (
        '{"check":"stress_cpu","status":"pass",'
        '"detail":"logical_cpus=128|duration_s=120|tool=stress-ng'
        '|peak_temp_c=72|max_freq_mhz=3600|min_freq_mhz_under_load=3550'
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
        '|peak_temp_c=85|avg_util_pct=98|throttle_sample_count=3'
        '|WARN:freq_throttle_detected_3_samples"}'
    )
    data = json.loads(sample)
    assert data["status"] == "warn"
    assert "WARN:freq_throttle_detected_3_samples" in data["detail"]

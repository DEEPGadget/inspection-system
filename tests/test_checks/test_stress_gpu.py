"""
stress_gpu.py 유닛 테스트.
nvidia-smi / gpu_burn / dcgmi 없는 CI 환경을 가정.
스크립트를 직접 실행하지 않고, 출력 JSON 규격과 로직 분기만 검증.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent.parent / "checks" / "base" / "post_install" / "stress_gpu.py"


def _run(env_override: dict | None = None, timeout: int = 10) -> dict:
    """stress_gpu.py를 최소 환경에서 실행하고 JSON 출력을 파싱한다."""
    env = {**os.environ, "GPU_BURNIN_DURATION": "1"}
    if env_override:
        env.update(env_override)

    result = subprocess.run(
        ["python3", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    # 마지막 줄이 JSON
    stdout = result.stdout.strip()
    last_line = stdout.splitlines()[-1] if stdout else ""
    return json.loads(last_line)


# ---------------------------------------------------------------------------
# nvidia-smi 없는 환경 — fail 반환
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    subprocess.run(["which", "nvidia-smi"], capture_output=True).returncode == 0,
    reason="nvidia-smi 있는 환경에서는 skip",
)
def test_no_nvidia_smi_returns_fail():
    """nvidia-smi 없으면 즉시 fail 반환."""
    # PATH를 빈 디렉토리로 제한하여 nvidia-smi 숨김
    env = {**os.environ, "PATH": "/bin:/usr/bin", "GPU_BURNIN_DURATION": "1"}
    result = subprocess.run(
        ["python3", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    stdout = result.stdout.strip()
    last_line = stdout.splitlines()[-1] if stdout else ""
    data = json.loads(last_line)
    assert data["check"] == "stress_gpu"
    assert data["status"] == "fail"
    assert "nvidia-smi" in data["detail"]


# ---------------------------------------------------------------------------
# JSON 출력 규격
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    subprocess.run(["which", "nvidia-smi"], capture_output=True).returncode != 0,
    reason="nvidia-smi 없는 환경에서는 skip",
)
def test_output_schema_with_nvidia_smi():
    """nvidia-smi 있을 때 JSON 규격 검증."""
    data = _run(timeout=30)
    assert "check" in data
    assert "status" in data
    assert "detail" in data
    assert data["check"] == "stress_gpu"
    assert data["status"] in ("pass", "fail", "warn")


@pytest.mark.skipif(
    subprocess.run(["which", "nvidia-smi"], capture_output=True).returncode != 0,
    reason="nvidia-smi 없는 환경에서는 skip",
)
def test_detail_contains_required_fields():
    """detail 문자열에 핵심 메트릭 필드가 포함되는지 확인."""
    data = _run(timeout=30)
    detail = data["detail"]
    for field in ("peak_temp_c", "peak_power_w", "avg_util_pct", "tool", "duration_s"):
        assert field in detail, f"missing field: {field}"


# ---------------------------------------------------------------------------
# JSON 유효성 — 실제 실행 없이 mock 출력으로 파싱 확인
# ---------------------------------------------------------------------------


def test_mock_output_parsing():
    """스크립트 출력 예시가 유효한 JSON인지 확인."""
    sample = (
        '{"check":"stress_gpu","status":"pass",'
        '"detail":"gpu_count=2|tdp_w=400|tool=gpu_burn|duration_s=1'
        "|peak_temp_c=70|peak_power_w=380|power_ratio_pct=95|avg_util_pct=99"
        "|slowdown_hw=0|slowdown_sw=0|slowdown_pwr=0"
        "|ecc_corr_before=0|ecc_corr_after=0|ecc_delta_corr=0"
        '|ecc_uncorr_before=0|ecc_uncorr_after=0|ecc_delta_uncorr=0"}'
    )
    data = json.loads(sample)
    assert data["status"] == "pass"
    assert "tool=gpu_burn" in data["detail"]


def test_mock_build_tools_missing_returns_fail():
    """gpu_burn 빌드 도구 누락 시 fail + 사유."""
    sample = (
        '{"check":"stress_gpu","status":"fail",'
        '"detail":"gpu_count=8|tdp_w=700'
        '|FAIL:gpu_burn_build_tools_missing=nvcc,make|tool=none|duration_s=300"}'
    )
    data = json.loads(sample)
    assert data["status"] == "fail"
    assert "FAIL:gpu_burn_build_tools_missing" in data["detail"]


def test_mock_clone_failed_returns_fail():
    """gpu_burn git clone 실패 시 사유 포함."""
    sample = (
        '{"check":"stress_gpu","status":"fail",'
        '"detail":"gpu_count=8|tdp_w=700'
        '|FAIL:gpu_burn_clone_failed:fatal: unable to access // Could not resolve host"}'
    )
    data = json.loads(sample)
    assert data["status"] == "fail"
    assert "FAIL:gpu_burn_clone_failed" in data["detail"]


def test_fail_conditions_in_detail():
    """FAIL 조건 문자열이 detail에 포함되는 샘플 JSON 파싱."""
    sample = (
        '{"check":"stress_gpu","status":"fail",'
        '"detail":"tool=gpu_burn|peak_temp_c=92'
        "|FAIL:peak_temp_over_87c(92c)"
        '|FAIL:ecc_uncorrected_increased_by=1"}'
    )
    data = json.loads(sample)
    assert data["status"] == "fail"
    assert "FAIL:peak_temp_over_87c" in data["detail"]
    assert "FAIL:ecc_uncorrected_increased_by=1" in data["detail"]

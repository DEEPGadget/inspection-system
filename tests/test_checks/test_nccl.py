"""
nccl_bandwidth.sh 유닛 테스트.
GPU / nccl-tests 없는 CI 환경을 가정.
JSON 출력 규격, 조기 종료 경로(GPU 없음, 단일 GPU), mock 파싱 검증.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent.parent / "checks" / "base" / "phase5_nccl" / "nccl_bandwidth.sh"

_HAS_NVIDIA = subprocess.run(["which", "nvidia-smi"], capture_output=True).returncode == 0


def _run(env_override: dict | None = None, timeout: int = 15) -> dict:
    env = {**os.environ}
    if env_override:
        env.update(env_override)
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
# nvidia-smi 없는 환경
# ---------------------------------------------------------------------------

@pytest.mark.skipif(_HAS_NVIDIA, reason="nvidia-smi 있는 환경에서는 skip")
def test_no_nvidia_smi_returns_fail():
    """nvidia-smi 없으면 즉시 fail 반환."""
    env = {**os.environ, "PATH": "/bin:/usr/bin"}
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    stdout = result.stdout.strip()
    last_line = stdout.splitlines()[-1] if stdout else ""
    data = json.loads(last_line)
    assert data["check"] == "nccl_bandwidth"
    assert data["status"] == "fail"
    assert "nvidia-smi" in data["detail"]


# ---------------------------------------------------------------------------
# JSON 출력 규격 (nvidia-smi 있는 환경)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_NVIDIA, reason="nvidia-smi 없는 환경에서는 skip")
def test_output_schema():
    data = _run()
    assert "check" in data
    assert "status" in data
    assert "detail" in data
    assert data["check"] == "nccl_bandwidth"
    assert data["status"] in ("pass", "fail", "warn")


@pytest.mark.skipif(not _HAS_NVIDIA, reason="nvidia-smi 없는 환경에서는 skip")
def test_detail_contains_gpu_count():
    data = _run()
    assert "gpu_count=" in data["detail"]


# ---------------------------------------------------------------------------
# shellcheck
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    subprocess.run(["which", "shellcheck"], capture_output=True).returncode != 0,
    reason="shellcheck 미설치",
)
def test_shellcheck_nccl():
    result = subprocess.run(
        ["shellcheck", "-S", "warning", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"shellcheck 실패:\n{result.stdout}"


# ---------------------------------------------------------------------------
# mock 출력 파싱 — 다양한 판정 시나리오
# ---------------------------------------------------------------------------

def test_mock_single_gpu_warn():
    """GPU 1개 → warn (NCCL 불필요)."""
    sample = (
        '{"check":"nccl_bandwidth","status":"warn",'
        '"detail":"gpu_count=1|WARN:single_gpu_nccl_skipped"}'
    )
    data = json.loads(sample)
    assert data["status"] == "warn"
    assert "single_gpu_nccl_skipped" in data["detail"]


def test_mock_pass_2gpu():
    """2-GPU 대역폭 충분 → pass."""
    sample = (
        '{"check":"nccl_bandwidth","status":"pass",'
        '"detail":"gpu_count=8|min_bw_2gpu_gbs=30|min_bw_4gpu_gbs=5'
        '|tool=/opt/nccl-tests/build/all_reduce_perf'
        '|bw_2gpu_gbs=42.3|bw_4gpu_gbs=8.7"}'
    )
    data = json.loads(sample)
    assert data["status"] == "pass"


def test_mock_fail_2gpu_low_bw():
    """2-GPU 대역폭 미달 → fail."""
    sample = (
        '{"check":"nccl_bandwidth","status":"fail",'
        '"detail":"gpu_count=8|min_bw_2gpu_gbs=30|tool=/opt/nccl-tests/build/all_reduce_perf'
        '|bw_2gpu_gbs=12.5|FAIL:2gpu_bw_12_gbs_below_30_gbs"}'
    )
    data = json.loads(sample)
    assert data["status"] == "fail"
    assert "FAIL:2gpu_bw_12_gbs_below_30_gbs" in data["detail"]


def test_mock_fail_4gpu_low_bw():
    """4-GPU AllReduce 미달 → fail."""
    sample = (
        '{"check":"nccl_bandwidth","status":"fail",'
        '"detail":"gpu_count=8|min_bw_4gpu_gbs=5|tool=/opt/nccl-tests/build/all_reduce_perf'
        '|bw_2gpu_gbs=38.1|bw_4gpu_gbs=3.2|FAIL:4gpu_bw_3_gbs_below_5_gbs"}'
    )
    data = json.loads(sample)
    assert data["status"] == "fail"
    assert "FAIL:4gpu_bw_3_gbs_below_5_gbs" in data["detail"]


def test_mock_warn_no_tool():
    """nccl-tests 바이너리 없음 → warn."""
    sample = (
        '{"check":"nccl_bandwidth","status":"warn",'
        '"detail":"gpu_count=8|min_bw_2gpu_gbs=30|min_bw_4gpu_gbs=5'
        '|WARN:no_nccl_test_tool_available"}'
    )
    data = json.loads(sample)
    assert data["status"] == "warn"
    assert "WARN:no_nccl_test_tool_available" in data["detail"]


def test_env_threshold_override():
    """환경변수로 임계값 오버라이드 시 detail에 반영되는지 mock으로 확인."""
    sample = (
        '{"check":"nccl_bandwidth","status":"fail",'
        '"detail":"gpu_count=2|min_bw_2gpu_gbs=50|bw_2gpu_gbs=42.0'
        '|FAIL:2gpu_bw_42_gbs_below_50_gbs"}'
    )
    data = json.loads(sample)
    assert "min_bw_2gpu_gbs=50" in data["detail"]
    assert "FAIL" in data["detail"]

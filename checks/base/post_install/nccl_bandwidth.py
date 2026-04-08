#!/usr/bin/env python3
"""nccl_bandwidth — NCCL 대역폭 / AllReduce 테스트 (nccl-tests 전용)
Phase5: 멀티 GPU 간 통신 성능 검증

기대 동작:
  1. ~/nccl-tests/build/all_reduce_perf 바이너리 확인
  2. 없으면 git clone → make 빌드 (~ 하위, sudo 불필요)
  3. all_reduce_perf 실행 → 2-GPU / 4-GPU 대역폭 측정
  4. 빌드/실행 실패 시 사유(stderr 마지막 5줄)와 함께 fail 반환

환경변수:
  NCCL_ALLREDUCE_MIN_BW_2GPU   2-GPU NVLink 최소 대역폭 GB/s [기본: 30]
  NCCL_ALLREDUCE_MIN_BW_4GPU   4-GPU AllReduce 최소 대역폭 GB/s [기본: 5]
  NCCL_TEST_SIZE               테스트 데이터 크기 [기본: 1G]

FAIL: 2-GPU busbw < min | 4-GPU busbw < min | 빌드/실행 실패
WARN: GPU < 2 → skip
출력: {"check":"nccl_bandwidth","status":"pass|fail|warn","detail":"..."}
"""

import json
import os
import subprocess
import sys

CHECK = "nccl_bandwidth"
NCCL_TESTS_DIR = os.path.expanduser("~/nccl-tests")
NCCL_BIN = f"{NCCL_TESTS_DIR}/build/all_reduce_perf"
NCCL_REPO = "https://github.com/NVIDIA/nccl-tests.git"


def run(cmd, timeout=10, env=None):
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env
        )
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", -1
    except Exception:
        return "", -1


def run_full(cmd, timeout=10, env=None):
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        return -1, "", f"timeout after {timeout}s: {e}"
    except Exception as e:
        return -1, "", str(e)


def tail_lines(text: str, n: int = 5) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return " // ".join(lines[-n:])


def emit(status, details):
    print(
        json.dumps(
            {"check": CHECK, "status": status, "detail": "|".join(details)}, ensure_ascii=False
        )
    )
    sys.exit(0)


def ensure_nccl_tests(details: list[str]) -> str | None:
    """all_reduce_perf 경로 반환. 확보 실패 시 details에 사유 추가하고 None 반환."""
    if os.path.isfile(NCCL_BIN) and os.access(NCCL_BIN, os.X_OK):
        return NCCL_BIN

    missing = [t for t in ("nvcc", "git", "make") if not run(f"command -v {t}")[0]]
    if missing:
        details.append(f"FAIL:nccl_tests_build_tools_missing={','.join(missing)}")
        return None

    subprocess.run(f"rm -rf {NCCL_TESTS_DIR}", shell=True)

    rc, _, err = run_full(f"git clone --depth=1 {NCCL_REPO} {NCCL_TESTS_DIR}", timeout=120)
    if rc != 0:
        details.append(f"FAIL:nccl_tests_clone_failed:{tail_lines(err)}")
        return None

    rc, _, err = run_full(f"make -C {NCCL_TESTS_DIR}", timeout=600)
    if rc != 0:
        details.append(f"FAIL:nccl_tests_make_failed:{tail_lines(err)}")
        return None

    if os.path.isfile(NCCL_BIN) and os.access(NCCL_BIN, os.X_OK):
        return NCCL_BIN

    details.append("FAIL:nccl_tests_binary_missing_after_build")
    return None


def main():
    status = "pass"
    details = []

    min_bw_2gpu = float(os.environ.get("NCCL_ALLREDUCE_MIN_BW_2GPU", "30"))
    min_bw_4gpu = float(os.environ.get("NCCL_ALLREDUCE_MIN_BW_4GPU", "5"))
    test_size = os.environ.get("NCCL_TEST_SIZE", "1G")

    _, rc = run("nvidia-smi", timeout=10)
    if rc != 0:
        emit("fail", ["FAIL:nvidia-smi not found"])

    gpu_out, _ = run("nvidia-smi --query-gpu=name --format=csv,noheader", timeout=10)
    gpu_count = len([line for line in gpu_out.splitlines() if line.strip()]) if gpu_out else 0
    details.append(f"gpu_count={gpu_count}")

    if gpu_count < 2:
        details.append("WARN:single_gpu_nccl_skipped")
        emit("warn", details)

    details += [f"min_bw_2gpu_gbs={min_bw_2gpu}", f"min_bw_4gpu_gbs={min_bw_4gpu}"]

    allreduce_bin = ensure_nccl_tests(details)
    if allreduce_bin is None:
        emit("fail", details)

    details.append(f"tool={allreduce_bin}")

    def measure_allreduce(gpu_list, ngpu, timeout_s=300):
        gpu_env = os.environ.copy()
        gpu_env["CUDA_VISIBLE_DEVICES"] = gpu_list

        rc, out, err = run_full(
            f"{allreduce_bin} -b 1M -e {test_size} -f 2 -g {ngpu} -n 20",
            timeout=timeout_s,
            env=gpu_env,
        )
        if rc != 0:
            return None, tail_lines(err or out)

        # 마지막 데이터 라인 추출 (공백+숫자로 시작)
        last = ""
        for line in out.splitlines():
            if line and line.lstrip()[:1].isdigit():
                last = line
        if not last:
            return None, "no data line in output"
        parts = last.split()
        try:
            return float(parts[7]), ""  # out-of-place busbw
        except Exception:
            return None, f"parse failed: {last[:80]}"

    # 2-GPU
    bw_2gpu, err_2 = measure_allreduce("0,1", 2)
    if bw_2gpu is None:
        details.append("bw_2gpu_gbs=N/A")
        status = "fail"
        details.append(f"FAIL:2gpu_bw_measurement_failed:{err_2}")
    else:
        details.append(f"bw_2gpu_gbs={bw_2gpu:.2f}")
        if bw_2gpu < min_bw_2gpu:
            status = "fail"
            details.append(f"FAIL:2gpu_bw_{bw_2gpu:.0f}_gbs_below_{min_bw_2gpu:.0f}_gbs")

    # 4-GPU
    if gpu_count >= 4:
        bw_4gpu, err_4 = measure_allreduce("0,1,2,3", 4)
        if bw_4gpu is None:
            details.append("bw_4gpu_gbs=N/A")
            status = "fail"
            details.append(f"FAIL:4gpu_bw_measurement_failed:{err_4}")
        else:
            details.append(f"bw_4gpu_gbs={bw_4gpu:.2f}")
            if bw_4gpu < min_bw_4gpu:
                status = "fail"
                details.append(f"FAIL:4gpu_bw_{bw_4gpu:.0f}_gbs_below_{min_bw_4gpu:.0f}_gbs")
    else:
        details.append(f"bw_4gpu_gbs=skipped(gpu_count={gpu_count})")

    emit(status, details)


if __name__ == "__main__":
    main()

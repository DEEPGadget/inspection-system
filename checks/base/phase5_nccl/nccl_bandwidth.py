#!/usr/bin/env python3
"""nccl_bandwidth — NCCL 대역폭 / AllReduce 테스트
Phase5: 멀티 GPU 간 통신 성능 검증

환경변수:
  NCCL_ALLREDUCE_MIN_BW_2GPU   2-GPU NVLink 최소 대역폭 GB/s [기본: 30]
  NCCL_ALLREDUCE_MIN_BW_4GPU   4-GPU AllReduce 최소 대역폭 GB/s [기본: 5]
  NCCL_TEST_SIZE               테스트 데이터 크기 [기본: 1G]

FAIL: 2-GPU busbw < NCCL_ALLREDUCE_MIN_BW_2GPU
      4-GPU busbw < NCCL_ALLREDUCE_MIN_BW_4GPU
WARN: GPU < 2 | nccl-tests 없음 | pytorch NCCL 없음
출력: {"check":"nccl_bandwidth","status":"pass|fail|warn","detail":"..."}
"""
import json
import os
import subprocess
import sys

CHECK = "nccl_bandwidth"
NCCL_TESTS_DIR = "/opt/nccl-tests"


def run(cmd, timeout=10, env=None):
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout,
            env=env,
        )
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", -1
    except Exception:
        return "", -1


def emit(status, details):
    print(json.dumps({"check": CHECK, "status": status, "detail": "|".join(details)}, ensure_ascii=False))
    sys.exit(0)


def main():
    status = "pass"
    details = []

    min_bw_2gpu = float(os.environ.get("NCCL_ALLREDUCE_MIN_BW_2GPU", "30"))
    min_bw_4gpu = float(os.environ.get("NCCL_ALLREDUCE_MIN_BW_4GPU", "5"))
    test_size = os.environ.get("NCCL_TEST_SIZE", "1G")

    # nvidia-smi 확인
    _, rc = run("nvidia-smi", timeout=10)
    if rc != 0:
        emit("fail", ["nvidia-smi not found"])

    # GPU 수량
    gpu_out, _ = run("nvidia-smi --query-gpu=name --format=csv,noheader", timeout=10)
    gpu_count = len([l for l in gpu_out.splitlines() if l.strip()]) if gpu_out else 0
    details.append(f"gpu_count={gpu_count}")

    if gpu_count < 2:
        if status == "pass":
            status = "warn"
        details.append("WARN:single_gpu_nccl_skipped")
        emit(status, details)

    details += [f"min_bw_2gpu_gbs={min_bw_2gpu}", f"min_bw_4gpu_gbs={min_bw_4gpu}"]

    # all_reduce_perf 바이너리 탐색
    allreduce_bin = None
    candidates = [
        "/usr/local/bin/all_reduce_perf",
        "/usr/bin/all_reduce_perf",
        f"{NCCL_TESTS_DIR}/build/all_reduce_perf",
        "/opt/nccl_tests/build/all_reduce_perf",
        "/root/nccl-tests/build/all_reduce_perf",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            allreduce_bin = c
            break

    # 없으면 github 빌드 시도
    if not allreduce_bin:
        print("all_reduce_perf not found — attempting build from github", file=sys.stderr)
        nvcc_ok, rc = run("command -v nvcc && command -v git && command -v make", timeout=5)
        if rc == 0:
            subprocess.run(f"rm -rf {NCCL_TESTS_DIR}", shell=True)
            r, _ = run(
                f"git clone --depth=1 https://github.com/NVIDIA/nccl-tests.git {NCCL_TESTS_DIR} "
                f"&& make -C {NCCL_TESTS_DIR}",
                timeout=300,
            )
            built = f"{NCCL_TESTS_DIR}/build/all_reduce_perf"
            if os.path.isfile(built) and os.access(built, os.X_OK):
                allreduce_bin = built
        else:
            print("git/make/nvcc not available — cannot build nccl-tests", file=sys.stderr)

    # PyTorch NCCL 사용 가능 여부
    pytorch_ok = False
    if not allreduce_bin:
        _, rc = run(
            "python3 -c \"import torch; assert torch.cuda.is_available(); import torch.distributed\"",
            timeout=15,
        )
        pytorch_ok = rc == 0

    if not allreduce_bin and not pytorch_ok:
        if status == "pass":
            status = "warn"
        details.append("WARN:no_nccl_test_tool_available")
        emit(status, details)

    tool = allreduce_bin if allreduce_bin else "pytorch_nccl"
    details.append(f"tool={tool}")

    def measure_allreduce(gpu_list, ngpu, timeout_s=120):
        """대역폭 측정. 성공 시 float GB/s, 실패 시 None 반환."""
        gpu_env = os.environ.copy()
        gpu_env["CUDA_VISIBLE_DEVICES"] = gpu_list

        if allreduce_bin:
            out, rc = run(
                f"{allreduce_bin} -b 1M -e {test_size} -f 2 -g {ngpu} -n 20 2>/dev/null"
                f" | grep -E '^\\s+[0-9]' | tail -1",
                timeout=timeout_s,
                env=gpu_env,
            )
            if not out:
                return None
            # 컬럼 8번째: out-of-place busbw
            parts = out.split()
            try:
                return float(parts[7])
            except Exception:
                return None
        else:
            # PyTorch NCCL 백업
            py_script = f"""
import torch, torch.distributed as dist, time, os
dist.init_process_group("nccl")
rank = dist.get_rank()
dev = torch.device(f"cuda:{{rank}}")
n = dist.get_world_size()
buf = torch.ones(1024*1024*64, dtype=torch.float32, device=dev)
for _ in range(3):
    dist.all_reduce(buf)
torch.cuda.synchronize()
t0 = time.perf_counter()
for _ in range(10):
    dist.all_reduce(buf)
torch.cuda.synchronize()
elapsed = time.perf_counter() - t0
if rank == 0:
    size_gb = buf.nelement() * buf.element_size() / 1e9
    bus_bw = 2 * (n - 1) / n * size_gb * 10 / elapsed
    print(f"{{bus_bw:.2f}}")
dist.destroy_process_group()
"""
            out, rc = run(
                f"torchrun --nproc_per_node={ngpu} --master_port=29501 - <<'PYEOF'\n{py_script}\nPYEOF",
                timeout=timeout_s,
                env=gpu_env,
            )
            try:
                return float(out.strip().splitlines()[-1])
            except Exception:
                return None

    # 2-GPU 대역폭 측정
    bw_2gpu = measure_allreduce("0,1", 2)
    if bw_2gpu is None:
        details.append("bw_2gpu_gbs=N/A")
        if status == "pass":
            status = "warn"
        details.append("WARN:2gpu_bw_measurement_failed")
    else:
        details.append(f"bw_2gpu_gbs={bw_2gpu:.2f}")
        if bw_2gpu < min_bw_2gpu:
            status = "fail"
            details.append(f"FAIL:2gpu_bw_{bw_2gpu:.0f}_gbs_below_{min_bw_2gpu:.0f}_gbs")

    # 4-GPU 대역폭 측정 (GPU >= 4인 경우)
    if gpu_count >= 4:
        bw_4gpu = measure_allreduce("0,1,2,3", 4)
        if bw_4gpu is None:
            details.append("bw_4gpu_gbs=N/A")
            if status == "pass":
                status = "warn"
            details.append("WARN:4gpu_bw_measurement_failed")
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

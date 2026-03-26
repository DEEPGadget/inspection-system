#!/bin/bash
# nccl_bandwidth.sh — NCCL 대역폭 / AllReduce 테스트
# Phase5: 멀티 GPU 간 통신 성능 검증
#
# 환경변수:
#   NCCL_ALLREDUCE_MIN_BW_2GPU   2-GPU NVLink 최소 대역폭 GB/s [기본: 30]
#   NCCL_ALLREDUCE_MIN_BW_4GPU   4-GPU AllReduce 최소 대역폭 GB/s [기본: 5]
#   NCCL_TEST_SIZE               테스트 데이터 크기 [기본: 1G]
#
# FAIL: 2-GPU busbw < NCCL_ALLREDUCE_MIN_BW_2GPU
#       4-GPU busbw < NCCL_ALLREDUCE_MIN_BW_4GPU
# WARN: GPU < 2 (단일 GPU 서버) | nccl-tests 바이너리 없음 | pytorch NCCL 없음
# 출력: {"check":"nccl_bandwidth","status":"pass|fail|warn","detail":"..."}
set -euo pipefail

CHECK="nccl_bandwidth"
STATUS="pass"
DETAILS=()

MIN_BW_2GPU="${NCCL_ALLREDUCE_MIN_BW_2GPU:-30}"
MIN_BW_4GPU="${NCCL_ALLREDUCE_MIN_BW_4GPU:-5}"
TEST_SIZE="${NCCL_TEST_SIZE:-1G}"

# ── nvidia-smi 확인 ───────────────────────────────────────
if ! command -v nvidia-smi &>/dev/null; then
    printf '{"check":"%s","status":"fail","detail":"nvidia-smi not found"}\n' "$CHECK"
    exit 0
fi

# ── GPU 수량 ─────────────────────────────────────────────
GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l || echo "0")
DETAILS+=("gpu_count=${GPU_COUNT}")

if [[ "$GPU_COUNT" -lt 2 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:single_gpu_nccl_skipped")
    DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
    printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"
    exit 0
fi

DETAILS+=("min_bw_2gpu_gbs=${MIN_BW_2GPU}" "min_bw_4gpu_gbs=${MIN_BW_4GPU}")

# ── all_reduce_perf 바이너리 탐색 ────────────────────────
ALLREDUCE_BIN=""
for candidate in \
    /usr/local/bin/all_reduce_perf \
    /usr/bin/all_reduce_perf \
    /opt/nccl-tests/build/all_reduce_perf \
    /opt/nccl_tests/build/all_reduce_perf \
    /root/nccl-tests/build/all_reduce_perf; do
    if [[ -x "$candidate" ]]; then
        ALLREDUCE_BIN="$candidate"
        break
    fi
done

# ── 2-GPU 테스트 ─────────────────────────────────────────
run_allreduce() {
    local gpu_list="$1"       # e.g. "0,1"
    local ngpu="$2"           # e.g. 2
    local result_var="$3"     # 변수명

    if [[ -n "$ALLREDUCE_BIN" ]]; then
        # nccl-tests all_reduce_perf
        local raw
        raw=$(CUDA_VISIBLE_DEVICES="$gpu_list" \
              "$ALLREDUCE_BIN" -b 1M -e "$TEST_SIZE" -f 2 -g "$ngpu" -n 20 2>/dev/null | \
              grep -E "^\s+[0-9]" | tail -1 || true)
        # 출력 컬럼: size count type redop root time algbw busbw #wrong time algbw busbw
        # busbw: 11번째 컬럼
        local bw
        bw=$(echo "$raw" | awk '{print $11}' | tr -d ' ' || echo "0")
        printf -v "$result_var" "%s" "${bw:-0}"

    elif python3 -c "import torch; assert torch.cuda.is_available(); import torch.distributed" 2>/dev/null; then
        # PyTorch NCCL 백업 (간이 AllReduce 측정)
        local bw
        bw=$(CUDA_VISIBLE_DEVICES="$gpu_list" python3 - <<PYEOF 2>/dev/null
import torch, torch.distributed as dist, time, os
os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
os.environ.setdefault("MASTER_PORT", "29501")
world = $ngpu
dist.init_process_group("nccl", rank=0, world_size=1)
dev = torch.device("cuda:0")
buf = torch.ones(1024*1024*64, dtype=torch.float32, device=dev)  # 256 MB
# warm-up
for _ in range(3):
    dist.all_reduce(buf)
torch.cuda.synchronize()
t0 = time.perf_counter()
for _ in range(10):
    dist.all_reduce(buf)
torch.cuda.synchronize()
elapsed = time.perf_counter() - t0
# busbw = 2*(n-1)/n * size / time  (allreduce bus BW formula)
n = $ngpu
size_gb = buf.nelement() * buf.element_size() / 1e9
bus_bw = 2 * (n-1) / n * size_gb * 10 / elapsed
print(f"{bus_bw:.2f}")
dist.destroy_process_group()
PYEOF
        )
        printf -v "$result_var" "%s" "${bw:-0}"
    else
        printf -v "$result_var" "%s" "N/A"
    fi
}

# nccl-tests 및 pytorch 모두 없으면 warn
if [[ -z "$ALLREDUCE_BIN" ]] && \
   ! python3 -c "import torch; assert torch.cuda.is_available(); import torch.distributed" 2>/dev/null; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:no_nccl_test_tool_available")
    DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
    printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"
    exit 0
fi

TOOL="${ALLREDUCE_BIN:-pytorch_nccl}"
DETAILS+=("tool=${TOOL}")

# ── 2-GPU 대역폭 측정 ────────────────────────────────────
BW_2GPU="0"
run_allreduce "0,1" 2 BW_2GPU
DETAILS+=("bw_2gpu_gbs=${BW_2GPU}")

if [[ "$BW_2GPU" == "N/A" ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:2gpu_bw_measurement_failed")
else
    BW_2GPU_INT=$(awk "BEGIN {printf \"%.0f\", ${BW_2GPU:-0}}")
    if [[ $BW_2GPU_INT -lt $MIN_BW_2GPU ]]; then
        STATUS="fail"
        DETAILS+=("FAIL:2gpu_bw_${BW_2GPU_INT}_gbs_below_${MIN_BW_2GPU}_gbs")
    fi
fi

# ── 4-GPU 대역폭 측정 (GPU >= 4인 경우) ─────────────────
if [[ "$GPU_COUNT" -ge 4 ]]; then
    BW_4GPU="0"
    run_allreduce "0,1,2,3" 4 BW_4GPU
    DETAILS+=("bw_4gpu_gbs=${BW_4GPU}")

    if [[ "$BW_4GPU" == "N/A" ]]; then
        [[ "$STATUS" == "pass" ]] && STATUS="warn"
        DETAILS+=("WARN:4gpu_bw_measurement_failed")
    else
        BW_4GPU_INT=$(awk "BEGIN {printf \"%.0f\", ${BW_4GPU:-0}}")
        if [[ $BW_4GPU_INT -lt $MIN_BW_4GPU ]]; then
            STATUS="fail"
            DETAILS+=("FAIL:4gpu_bw_${BW_4GPU_INT}_gbs_below_${MIN_BW_4GPU}_gbs")
        fi
    fi
else
    DETAILS+=("bw_4gpu_gbs=skipped(gpu_count=${GPU_COUNT})")
fi

DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"

#!/bin/bash
# stress_gpu.sh — GPU 스트레스 테스트
# Phase4: 부하 중 온도/전력/Utilization/Slowdown/ECC 모니터링
#
# 환경변수:
#   GPU_BURNIN_DURATION  부하 지속 시간(초) [기본: 300]
#
# FAIL: peak_temp > 87°C | HW throttle 발생 | ECC uncorrected 증가
#       풀로드(util≥80%) + 저전력(power_ratio<70%)
# WARN: SW/PWR throttle | ECC corrected 증가 | util<80% | 도구 없음
# 출력: {"check":"stress_gpu","status":"pass|fail|warn","detail":"..."}
set -euo pipefail

CHECK="stress_gpu"
STATUS="pass"
DETAILS=()
DURATION="${GPU_BURNIN_DURATION:-300}"

# ── nvidia-smi 확인 ───────────────────────────────────────
if ! command -v nvidia-smi &>/dev/null; then
    printf '{"check":"%s","status":"fail","detail":"nvidia-smi not found"}\n' "$CHECK"
    exit 0
fi

# ── GPU 수량 확인 ─────────────────────────────────────────
GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l || echo "0")
if [[ "$GPU_COUNT" -eq 0 ]]; then
    printf '{"check":"%s","status":"fail","detail":"no GPUs detected"}\n' "$CHECK"
    exit 0
fi
DETAILS+=("gpu_count=${GPU_COUNT}")

# ── TDP 조회 ─────────────────────────────────────────────
TDP=$(nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits 2>/dev/null \
    | head -1 | tr -d ' ')
TDP="${TDP:-0}"
DETAILS+=("tdp_w=${TDP}")

# ── ECC 기준값 스냅샷 (부하 전) ──────────────────────────
ECC_CORR_BEFORE=$(nvidia-smi --query-gpu=ecc.errors.corrected.volatile.total \
    --format=csv,noheader,nounits 2>/dev/null \
    | grep -v "N/A" | awk '{s+=$1} END {print s+0}')
ECC_UNCORR_BEFORE=$(nvidia-smi --query-gpu=ecc.errors.uncorrected.volatile.total \
    --format=csv,noheader,nounits 2>/dev/null \
    | grep -v "N/A" | awk '{s+=$1} END {print s+0}')
ECC_CORR_BEFORE="${ECC_CORR_BEFORE:-0}"
ECC_UNCORR_BEFORE="${ECC_UNCORR_BEFORE:-0}"

# ── 스트레스 도구 탐색 및 실행 ────────────────────────────
TOOL="none"
STRESS_PID=""
GPU_BURN_DIR="/opt/gpu-burn"

# ── trap: 종료 시 stress 프로세스 정리 ─────────────────────
trap '[[ -n "${STRESS_PID:-}" ]] && kill "${STRESS_PID}" 2>/dev/null || true' EXIT

# 1) 호스트에 gpu_burn 바이너리가 있으면 직접 사용
if command -v gpu_burn &>/dev/null; then
    TOOL="gpu_burn"
    gpu_burn "$DURATION" >/dev/null 2>&1 &
    STRESS_PID=$!
elif [[ -x "${GPU_BURN_DIR}/gpu_burn" ]]; then
    TOOL="gpu_burn"
    "${GPU_BURN_DIR}/gpu_burn" "$DURATION" >/dev/null 2>&1 &
    STRESS_PID=$!

# 2) nvcc 있으면 소스 빌드 (git/make는 이미 설치돼 있어야 함)
elif command -v nvcc &>/dev/null; then
    echo "nvcc found — building gpu_burn from source" >&2
    if command -v git &>/dev/null && command -v make &>/dev/null; then
        rm -rf "${GPU_BURN_DIR}"
        git clone --depth=1 https://github.com/wilicc/gpu-burn.git "${GPU_BURN_DIR}" >/dev/null 2>&1 \
            && make -C "${GPU_BURN_DIR}" >/dev/null 2>&1 \
            && echo "gpu_burn build succeeded" >&2 \
            || echo "gpu_burn build failed — falling back" >&2
    fi
    if [[ -x "${GPU_BURN_DIR}/gpu_burn" ]]; then
        TOOL="gpu_burn"
        "${GPU_BURN_DIR}/gpu_burn" "$DURATION" >/dev/null 2>&1 &
        STRESS_PID=$!
    fi
fi

# 4) dcgmi
if [[ "$TOOL" == "none" ]] && command -v dcgmi &>/dev/null; then
    TOOL="dcgmi"
    dcgmi diag -r 2 >/dev/null 2>&1 &
    STRESS_PID=$!
fi

# 5) pytorch
if [[ "$TOOL" == "none" ]] && python3 -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
    TOOL="pytorch"
    python3 -c "
import time, torch
n = 8192
devs = [torch.device(f'cuda:{i}') for i in range(torch.cuda.device_count())]
pairs = [(torch.randn(n, n, device=d), torch.randn(n, n, device=d)) for d in devs]
end_t = time.time() + $DURATION
while time.time() < end_t:
    for a, b in pairs:
        torch.matmul(a, b)
" >/dev/null 2>&1 &
    STRESS_PID=$!
fi

if [[ "$TOOL" == "none" ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:no_stress_tool_temp_only")
fi

# ── 모니터링 루프 (10초 간격) ─────────────────────────────
PEAK_TEMP=0
PEAK_POWER=0
UTIL_SUM=0
SAMPLE_COUNT=0
SLOWDOWN_HW=0
SLOWDOWN_SW=0
SLOWDOWN_PWR=0
STRESS_DIED=0

END_TIME=$(( $(date +%s) + DURATION ))

while [[ $(date +%s) -lt $END_TIME ]]; do
    # stress 프로세스 생존 확인
    if [[ -n "$STRESS_PID" ]] && ! kill -0 "$STRESS_PID" 2>/dev/null; then
        echo "stress tool exited early (pid=${STRESS_PID})" >&2
        STRESS_DIED=1
        break
    fi

    # GPU별 메트릭 수집: index,temp,power,util,throttle
    while IFS=',' read -r _idx temp power util throttle; do
        temp=$(echo "$temp"     | tr -d ' ')
        power=$(echo "$power"   | tr -d ' ')
        util=$(echo "$util"     | tr -d ' ')
        throttle=$(echo "$throttle" | tr -d ' ')

        # 최고 온도
        [[ "$temp" =~ ^[0-9]+$ ]] && (( temp > PEAK_TEMP )) && PEAK_TEMP=$temp || true

        # 최고 전력 (소수점 제거)
        pwr_int="${power%.*}"
        [[ "$pwr_int" =~ ^[0-9]+$ ]] && (( pwr_int > PEAK_POWER )) && PEAK_POWER=$pwr_int || true

        # Utilization 누적 (평균 계산용)
        if [[ "$util" =~ ^[0-9]+$ ]]; then
            UTIL_SUM=$(( UTIL_SUM + util ))
            SAMPLE_COUNT=$(( SAMPLE_COUNT + 1 ))
        fi

        # Throttle 비트마스크 분류
        if [[ "$throttle" =~ ^0x ]]; then
            hex="${throttle#0x}"
            dec=$(( 16#${hex} ))
            if (( dec & 0x8 )); then SLOWDOWN_HW=$(( SLOWDOWN_HW + 1 )); fi
            if (( dec & 0x4 )); then SLOWDOWN_SW=$(( SLOWDOWN_SW + 1 )); fi
            if (( dec & 0x1 )); then SLOWDOWN_PWR=$(( SLOWDOWN_PWR + 1 )); fi
        fi
    done < <(nvidia-smi \
        --query-gpu=index,temperature.gpu,power.draw,utilization.gpu,clocks_throttle_reasons.active \
        --format=csv,noheader,nounits 2>/dev/null || true)

    sleep 10
done

# stress 프로세스 정리
if [[ -n "$STRESS_PID" ]]; then
    kill "$STRESS_PID" 2>/dev/null || true
    wait "$STRESS_PID" 2>/dev/null || true
fi

# ── ECC 사후 측정 (부하 후) ───────────────────────────────
ECC_CORR_AFTER=$(nvidia-smi --query-gpu=ecc.errors.corrected.volatile.total \
    --format=csv,noheader,nounits 2>/dev/null \
    | grep -v "N/A" | awk '{s+=$1} END {print s+0}')
ECC_UNCORR_AFTER=$(nvidia-smi --query-gpu=ecc.errors.uncorrected.volatile.total \
    --format=csv,noheader,nounits 2>/dev/null \
    | grep -v "N/A" | awk '{s+=$1} END {print s+0}')
ECC_CORR_AFTER="${ECC_CORR_AFTER:-0}"
ECC_UNCORR_AFTER="${ECC_UNCORR_AFTER:-0}"

ECC_DELTA_CORR=$(( ECC_CORR_AFTER - ECC_CORR_BEFORE ))
ECC_DELTA_UNCORR=$(( ECC_UNCORR_AFTER - ECC_UNCORR_BEFORE ))
# 카운터 리셋 등으로 음수가 되는 경우 0으로 처리
(( ECC_DELTA_CORR   < 0 )) && ECC_DELTA_CORR=0   || true
(( ECC_DELTA_UNCORR < 0 )) && ECC_DELTA_UNCORR=0 || true

# ── 평균 Utilization ─────────────────────────────────────
AVG_UTIL=0
[[ $SAMPLE_COUNT -gt 0 ]] && AVG_UTIL=$(( UTIL_SUM / SAMPLE_COUNT )) || true

# ── 전력 비율 (peak_power / TDP) ─────────────────────────
PWR_RATIO=0
if [[ "$TDP" =~ ^[0-9]+$ ]] && [[ $TDP -gt 0 ]]; then
    PWR_RATIO=$(awk "BEGIN {printf \"%.0f\", $PEAK_POWER / $TDP * 100}")
fi

DETAILS+=(
    "tool=${TOOL}"
    "duration_s=${DURATION}"
    "peak_temp_c=${PEAK_TEMP}"
    "peak_power_w=${PEAK_POWER}"
    "power_ratio_pct=${PWR_RATIO}"
    "avg_util_pct=${AVG_UTIL}"
    "slowdown_hw=${SLOWDOWN_HW}"
    "slowdown_sw=${SLOWDOWN_SW}"
    "slowdown_pwr=${SLOWDOWN_PWR}"
    "ecc_corr_before=${ECC_CORR_BEFORE}"
    "ecc_corr_after=${ECC_CORR_AFTER}"
    "ecc_delta_corr=${ECC_DELTA_CORR}"
    "ecc_uncorr_before=${ECC_UNCORR_BEFORE}"
    "ecc_uncorr_after=${ECC_UNCORR_AFTER}"
    "ecc_delta_uncorr=${ECC_DELTA_UNCORR}"
)

# ── FAIL 판정 ─────────────────────────────────────────────
if [[ $PEAK_TEMP -gt 87 ]]; then
    STATUS="fail"
    DETAILS+=("FAIL:peak_temp_over_87c(${PEAK_TEMP}c)")
fi
if [[ $SLOWDOWN_HW -gt 0 ]]; then
    STATUS="fail"
    DETAILS+=("FAIL:hw_thermal_throttle_count=${SLOWDOWN_HW}")
fi
if [[ $ECC_DELTA_UNCORR -gt 0 ]]; then
    STATUS="fail"
    DETAILS+=("FAIL:ecc_uncorrected_increased_by=${ECC_DELTA_UNCORR}")
fi
# 풀로드인데 전력 미달 → 전원부/VRM 이상 징후
if [[ $AVG_UTIL -ge 80 && $PWR_RATIO -lt 70 && $TDP -gt 0 ]]; then
    STATUS="fail"
    DETAILS+=("FAIL:full_load_low_power(util=${AVG_UTIL}pct,ratio=${PWR_RATIO}pct_of_tdp)")
fi

# ── WARN 판정 ─────────────────────────────────────────────
if [[ $SLOWDOWN_SW -gt 0 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:sw_thermal_slowdown_count=${SLOWDOWN_SW}")
fi
if [[ $SLOWDOWN_PWR -gt 0 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:power_cap_throttle_count=${SLOWDOWN_PWR}")
fi
if [[ $ECC_DELTA_CORR -gt 0 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:ecc_corrected_increased_by=${ECC_DELTA_CORR}")
fi
if [[ "$TOOL" != "none" && $AVG_UTIL -lt 80 && $SAMPLE_COUNT -gt 0 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:low_gpu_utilization_avg=${AVG_UTIL}pct")
fi
if [[ $STRESS_DIED -eq 1 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:stress_tool_exited_early")
fi

DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"

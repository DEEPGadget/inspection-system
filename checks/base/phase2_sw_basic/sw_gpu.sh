#!/bin/bash
# sw_gpu.sh — NVIDIA GPU 수량, 드라이버, 온도, VRAM 검사
# FAIL: nvidia-smi 없음, GPU 온도 > 87°C
# 출력: {"check":"sw_gpu","status":"pass|fail|warn","detail":"..."}
set -euo pipefail

CHECK="sw_gpu"
STATUS="pass"
DETAILS=()

# ── nvidia-smi 존재 확인 ─────────────────────────────────
if ! command -v nvidia-smi &>/dev/null; then
    printf '{"check":"%s","status":"fail","detail":"nvidia-smi not found"}\n' "$CHECK"
    exit 0
fi

# ── GPU 기본 정보 ────────────────────────────────────────
GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l || echo "0")
if [[ "$GPU_COUNT" -eq 0 ]]; then
    printf '{"check":"%s","status":"fail","detail":"no GPUs detected"}\n' "$CHECK"
    exit 0
fi
DETAILS+=("gpu_count=${GPU_COUNT}")

# ── 드라이버 버전 ────────────────────────────────────────
DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')
CUDA_VER=$(nvidia-smi 2>/dev/null | grep -oP "CUDA Version:\s+\K[\d.]+" | head -1 || echo "unknown")
DETAILS+=("driver=${DRIVER_VER}" "cuda=${CUDA_VER}")

# ── GPU 모델 목록 ────────────────────────────────────────
GPU_MODELS=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | sort | uniq -c \
    | awk '{print $2"_x"$1}' | tr '\n' ',' | sed 's/,$//')
DETAILS+=("models=${GPU_MODELS}")

# ── VRAM ─────────────────────────────────────────────────
VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
VRAM_GB=$(awk "BEGIN {printf \"%.0f\", ${VRAM_MB:-0}/1024}")
DETAILS+=("vram_per_gpu_gb=${VRAM_GB}")

# ── 온도 검사 (FAIL: > 87°C) ────────────────────────────
MAX_TEMP=0
while IFS= read -r temp; do
    temp=$(echo "$temp" | tr -d ' ')
    [[ -z "$temp" || "$temp" == "N/A" ]] && continue
    if [[ "$temp" -gt "$MAX_TEMP" ]]; then
        MAX_TEMP="$temp"
    fi
done < <(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null)

DETAILS+=("gpu_max_temp_c=${MAX_TEMP}")
if [[ "$MAX_TEMP" -gt 87 ]]; then
    STATUS="fail"
    DETAILS+=("FAIL:gpu_temp_over_87c")
fi

# ── 현재 전력 및 상태 ────────────────────────────────────
POWER_DRAW=$(nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits 2>/dev/null \
    | awk '{s+=$1} END {printf "%.0f", s}' || echo "?")
DETAILS+=("total_power_draw_w=${POWER_DRAW}")

# ── ECC (오류 카운트) ────────────────────────────────────
ECC_ERRORS=$(nvidia-smi --query-gpu=ecc.errors.uncorrected.volatile.total \
    --format=csv,noheader,nounits 2>/dev/null \
    | grep -v "N/A" | awk '{s+=$1} END {print s+0}' || echo "N/A")
DETAILS+=("ecc_uncorrected=${ECC_ERRORS}")
if [[ "$ECC_ERRORS" != "N/A" && "$ECC_ERRORS" -gt 0 ]]; then
    STATUS="fail"
    DETAILS+=("FAIL:ecc_uncorrected_errors")
fi

# ── NVLink 상태 (선택) ───────────────────────────────────
if nvidia-smi nvlink --status &>/dev/null 2>&1; then
    NVLINK_ACTIVE=$(nvidia-smi nvlink --status 2>/dev/null | grep -c "Active" || true)
    NVLINK_ACTIVE="${NVLINK_ACTIVE:-0}"
    DETAILS+=("nvlink_active_links=${NVLINK_ACTIVE}")
fi

# ── Persistence 모드 ─────────────────────────────────────
PERSIST=$(nvidia-smi --query-gpu=persistence_mode --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')
DETAILS+=("persistence_mode=${PERSIST}")
if [[ "$PERSIST" != "Enabled" ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:persistence_mode_disabled")
fi

DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"

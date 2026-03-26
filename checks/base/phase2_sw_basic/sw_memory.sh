#!/bin/bash
# sw_memory.sh — 메모리 용량 및 구성 확인
# 출력: {"check":"sw_memory","status":"pass|fail|warn","detail":"..."}
set -euo pipefail

CHECK="sw_memory"
STATUS="pass"
DETAILS=()

# ── 전체 / 가용 메모리 ───────────────────────────────────
TOTAL_KB=$(grep "^MemTotal:" /proc/meminfo | awk '{print $2}')
FREE_KB=$(grep "^MemAvailable:" /proc/meminfo | awk '{print $2}')
TOTAL_GB=$(awk "BEGIN {printf \"%.0f\", $TOTAL_KB/1024/1024}")
FREE_GB=$(awk "BEGIN  {printf \"%.0f\", $FREE_KB/1024/1024}")

DETAILS+=("total_gb=${TOTAL_GB}" "available_gb=${FREE_GB}")

# ── 스왑 ────────────────────────────────────────────────
SWAP_TOTAL_KB=$(grep "^SwapTotal:" /proc/meminfo | awk '{print $2}')
SWAP_FREE_KB=$(grep  "^SwapFree:"  /proc/meminfo | awk '{print $2}')
SWAP_TOTAL_GB=$(awk "BEGIN {printf \"%.0f\", $SWAP_TOTAL_KB/1024/1024}")
DETAILS+=("swap_total_gb=${SWAP_TOTAL_GB}")

# ── DIMM 슬롯 정보 (dmidecode 가능 시) ───────────────────
if command -v dmidecode &>/dev/null; then
    DIMM_COUNT=$(dmidecode -t memory 2>/dev/null | grep -c "Size:.*[0-9].*GB" || true)
    DIMM_COUNT="${DIMM_COUNT:-0}"
    DIMM_INFO=$(dmidecode -t memory 2>/dev/null \
        | grep -E "^\s+Size:|^\s+Speed:|^\s+Type:" \
        | grep -v "No Module" | head -20 \
        | tr -d '\t' | tr '\n' ',' | sed 's/,$//' || echo "")
    [[ -n "$DIMM_COUNT" ]] && DETAILS+=("dimm_populated=${DIMM_COUNT}")
fi

# ── NUMA 토폴로지 ────────────────────────────────────────
if [[ -d /sys/devices/system/node ]]; then
    NUMA_NODES=$(ls /sys/devices/system/node/ 2>/dev/null | grep -c "^node[0-9]" || true)
    NUMA_NODES="${NUMA_NODES:-1}"
    DETAILS+=("numa_nodes=${NUMA_NODES}")
fi

# ── ECC 상태 ────────────────────────────────────────────
ECC_STATUS="unknown"
if command -v edac-util &>/dev/null; then
    ECC_ERRORS=$(edac-util -s 0 2>/dev/null | grep -c "error" || true)
    ECC_ERRORS="${ECC_ERRORS:-0}"
    ECC_STATUS="errors=${ECC_ERRORS}"
elif [[ -d /sys/bus/platform/drivers/APEI ]]; then
    ECC_STATUS="apei_present"
fi
DETAILS+=("ecc=${ECC_STATUS}")

# ── 최소 용량 검사 (GPU 서버 기준 64GB 미만 warn) ─────────
if [[ "$TOTAL_GB" -lt 64 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:total_memory_below_64gb")
fi

DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"

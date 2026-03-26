#!/bin/bash
# sw_cpu.sh — CPU 정보 수집 및 온도 검사
# 출력: {"check":"sw_cpu","status":"pass|fail|warn","detail":"..."}
set -euo pipefail

CHECK="sw_cpu"
STATUS="pass"
DETAILS=()

# ── 모델명 ──────────────────────────────────────────────
MODEL=$(grep -m1 "model name" /proc/cpuinfo 2>/dev/null | cut -d: -f2 | sed 's/^ *//' || echo "unknown")

# ── 소켓 / 코어 / 스레드 ────────────────────────────────
SOCKETS=$(grep "physical id" /proc/cpuinfo 2>/dev/null | sort -u | wc -l || true)
SOCKETS="${SOCKETS:-1}"
CORES_PER_SOCKET=$(grep -m1 "cpu cores" /proc/cpuinfo 2>/dev/null | cut -d: -f2 | tr -d ' ' || true)
CORES_PER_SOCKET="${CORES_PER_SOCKET:-?}"
THREADS=$(grep -c "^processor" /proc/cpuinfo 2>/dev/null || true)
THREADS="${THREADS:-?}"

# ── 최대 주파수 (MHz) ────────────────────────────────────
MAX_FREQ_KHZ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null || echo "")
if [[ -n "$MAX_FREQ_KHZ" ]]; then
    MAX_FREQ_GHZ=$(awk "BEGIN {printf \"%.1f\", $MAX_FREQ_KHZ/1000000}")
else
    MAX_FREQ_GHZ=$(grep -m1 "cpu MHz" /proc/cpuinfo 2>/dev/null | cut -d: -f2 | sed 's/^ *//' \
        | awk '{printf "%.1f", $1/1000}' || echo "?")
fi

DETAILS+=("model=${MODEL}" "sockets=${SOCKETS}" "cores_per_socket=${CORES_PER_SOCKET}" \
          "threads=${THREADS}" "max_freq_ghz=${MAX_FREQ_GHZ}")

# ── CPU 온도 (FAIL: > 100°C) ─────────────────────────────
MAX_TEMP_C=""

# 방법 1: /sys/class/thermal/thermal_zone* (대부분의 서버)
TEMP_FILES=$(ls /sys/class/thermal/thermal_zone*/temp 2>/dev/null || true)
if [[ -n "$TEMP_FILES" ]]; then
    for f in $TEMP_FILES; do
        TYPE_FILE="${f%temp}type"
        TYPE=$(cat "$TYPE_FILE" 2>/dev/null || echo "")
        # x86_pkg_temp = 소켓 전체 온도, acpitz = ACPI 온도
        if [[ "$TYPE" == *"x86_pkg_temp"* || "$TYPE" == *"acpitz"* || "$TYPE" == *"cpu"* ]]; then
            RAW=$(cat "$f" 2>/dev/null || echo "")
            [[ -z "$RAW" ]] && continue
            TEMP_C=$(awk "BEGIN {printf \"%.0f\", $RAW/1000}")
            if [[ -z "$MAX_TEMP_C" || "$TEMP_C" -gt "$MAX_TEMP_C" ]]; then
                MAX_TEMP_C="$TEMP_C"
            fi
        fi
    done
fi

# 방법 2: sensors (lm-sensors) — 설치된 경우
if [[ -z "$MAX_TEMP_C" ]] && command -v sensors &>/dev/null; then
    SENS_TEMP=$(sensors 2>/dev/null | grep -oP 'Package id \d+:\s+\+\K[0-9.]+' | sort -n | tail -1 || true)
    [[ -n "$SENS_TEMP" ]] && MAX_TEMP_C=$(printf "%.0f" "$SENS_TEMP")
fi

if [[ -n "$MAX_TEMP_C" ]]; then
    DETAILS+=("cpu_max_temp_c=${MAX_TEMP_C}")
    if [[ "$MAX_TEMP_C" -gt 100 ]]; then
        STATUS="fail"
        DETAILS+=("FAIL:cpu_temp_over_100c")
    fi
else
    DETAILS+=("cpu_temp=unavailable")
    # 온도 읽기 실패는 warn (센서 없는 환경 가능)
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
fi

# ── 출력 ─────────────────────────────────────────────────
DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"

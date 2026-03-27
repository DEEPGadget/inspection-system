#!/bin/bash
# stress_cpu.sh — CPU 스트레스 테스트
# Phase4: 부하 중 온도/주파수/Utilization 모니터링
#
# 환경변수:
#   CPU_BURNIN_DURATION  부하 지속 시간(초) [기본: 120]
#
# FAIL: peak_temp > 100°C | 스트레스 도구 없이 온도 측정 불가
# WARN: SW throttle(주파수 강하) | 도구 없음 | util < 80%
# 출력: {"check":"stress_cpu","status":"pass|fail|warn","detail":"..."}
set -euo pipefail

CHECK="stress_cpu"
STATUS="pass"
DETAILS=()
DURATION="${CPU_BURNIN_DURATION:-120}"

NPROC=$(nproc 2>/dev/null || echo "1")
DETAILS+=("logical_cpus=${NPROC}" "duration_s=${DURATION}")

# ── 스트레스 도구 탐색 및 자동 설치 ──────────────────────
TOOL="none"
STRESS_PID=""

if ! command -v stress-ng &>/dev/null && ! command -v stress &>/dev/null; then
    echo "stress/stress-ng not found — falling back to python3" >&2
fi

if command -v stress-ng &>/dev/null; then
    TOOL="stress-ng"
    stress-ng --cpu "$NPROC" --cpu-method matrixprod --timeout "${DURATION}s" \
        >/dev/null 2>&1 &
    STRESS_PID=$!
elif command -v stress &>/dev/null; then
    TOOL="stress"
    stress --cpu "$NPROC" --timeout "${DURATION}" >/dev/null 2>&1 &
    STRESS_PID=$!
else
    if python3 -c "import sys" 2>/dev/null; then
        TOOL="python3"
        python3 -c "
import time, threading, sys
end_t = time.time() + $DURATION
def burn():
    while time.time() < end_t:
        x = sum(i * i for i in range(10000))
threads = [threading.Thread(target=burn) for _ in range($NPROC)]
for t in threads: t.start()
for t in threads: t.join()
" >/dev/null 2>&1 &
        STRESS_PID=$!
    fi
fi

if [[ "$TOOL" == "none" ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:no_stress_tool_temp_only")
fi

# ── trap: 종료 시 stress 프로세스 정리 ────────────────────
trap '[[ -n "${STRESS_PID:-}" ]] && kill "${STRESS_PID}" 2>/dev/null || true' EXIT

# ── 온도 파일 목록 결정 ─────────────────────────────────
TEMP_FILES=()
for f in /sys/class/thermal/thermal_zone*/temp; do
    [[ -f "$f" ]] || continue
    TYPE_FILE="${f%temp}type"
    TYPE=$(cat "$TYPE_FILE" 2>/dev/null || echo "")
    if [[ "$TYPE" == *"x86_pkg_temp"* || "$TYPE" == *"acpitz"* || "$TYPE" == *"cpu"* ]]; then
        TEMP_FILES+=("$f")
    fi
done

# ── 모니터링 루프 (5초 간격) ─────────────────────────────
PEAK_TEMP=0
MIN_FREQ_MHZ=999999
UTIL_SUM=0
SAMPLE_COUNT=0
THROTTLE_COUNT=0
STRESS_DIED=0

END_TIME=$(( $(date +%s) + DURATION ))

while [[ $(date +%s) -lt $END_TIME ]]; do
    # stress 프로세스 생존 확인
    if [[ -n "$STRESS_PID" ]] && ! kill -0 "$STRESS_PID" 2>/dev/null; then
        echo "stress tool exited early (pid=${STRESS_PID})" >&2
        STRESS_DIED=1
        break
    fi

    # ── CPU 온도 수집 ───────────────────────────────────
    for f in "${TEMP_FILES[@]+"${TEMP_FILES[@]}"}"; do
        RAW=$(cat "$f" 2>/dev/null || echo "")
        [[ -z "$RAW" || ! "$RAW" =~ ^[0-9]+$ ]] && continue
        TEMP_C=$(( RAW / 1000 ))
        (( TEMP_C > PEAK_TEMP )) && PEAK_TEMP=$TEMP_C
    done

    # sensors 백업 (TEMP_FILES가 비어있는 경우)
    if [[ ${#TEMP_FILES[@]} -eq 0 ]] && command -v sensors &>/dev/null; then
        SENS_TEMP=$(sensors 2>/dev/null \
            | grep -oP 'Package id \d+:\s+\+\K[0-9.]+' | sort -n | tail -1 || true)
        if [[ -n "$SENS_TEMP" ]]; then
            TEMP_C=$(printf "%.0f" "$SENS_TEMP")
            (( TEMP_C > PEAK_TEMP )) && PEAK_TEMP=$TEMP_C
        fi
    fi

    # ── 현재 주파수 (CPU0 기준, kHz) ────────────────────
    FREQ_KHZ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo "0")
    if [[ "$FREQ_KHZ" =~ ^[0-9]+$ && "$FREQ_KHZ" -gt 0 ]]; then
        FREQ_MHZ=$(( FREQ_KHZ / 1000 ))
        (( FREQ_MHZ < MIN_FREQ_MHZ )) && MIN_FREQ_MHZ=$FREQ_MHZ

        # 최대 주파수 대비 80% 미만이면 throttle 의심
        MAX_FREQ_KHZ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null || echo "0")
        if [[ "$MAX_FREQ_KHZ" =~ ^[0-9]+$ && "$MAX_FREQ_KHZ" -gt 0 ]]; then
            RATIO=$(awk "BEGIN {printf \"%.0f\", $FREQ_KHZ / $MAX_FREQ_KHZ * 100}")
            (( RATIO < 80 )) && THROTTLE_COUNT=$(( THROTTLE_COUNT + 1 ))
        fi
    fi

    # ── CPU Utilization (/proc/stat) ─────────────────────
    if [[ -f /proc/stat ]]; then
        read -r _ user nice system idle iowait irq softirq _ < /proc/stat || true
        if [[ "$idle" =~ ^[0-9]+$ ]]; then
            sleep 1
            read -r _ user2 nice2 system2 idle2 iowait2 irq2 softirq2 _ < /proc/stat || true
            d_idle=$(( idle2 - idle ))
            d_total=$(( (user2+nice2+system2+idle2+iowait2+irq2+softirq2) \
                        - (user+nice+system+idle+iowait+irq+softirq) ))
            if [[ $d_total -gt 0 ]]; then
                UTIL=$(awk "BEGIN {printf \"%.0f\", (1 - $d_idle/$d_total)*100}")
                if [[ "$UTIL" =~ ^[0-9]+$ ]]; then
                    UTIL_SUM=$(( UTIL_SUM + UTIL ))
                    SAMPLE_COUNT=$(( SAMPLE_COUNT + 1 ))
                fi
            fi
        fi
    fi

    sleep 4
done

# stress 프로세스 정리
if [[ -n "$STRESS_PID" ]]; then
    kill "$STRESS_PID" 2>/dev/null || true
    wait "$STRESS_PID" 2>/dev/null || true
fi

# ── 평균 Utilization ─────────────────────────────────────
AVG_UTIL=0
[[ $SAMPLE_COUNT -gt 0 ]] && AVG_UTIL=$(( UTIL_SUM / SAMPLE_COUNT )) || true

# ── 최소 주파수 정리 ─────────────────────────────────────
[[ $MIN_FREQ_MHZ -eq 999999 ]] && MIN_FREQ_MHZ=0

# ── 최대 주파수 (MHz) ────────────────────────────────────
MAX_FREQ_MHZ=0
MAX_FREQ_KHZ_VAL=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null || echo "0")
[[ "$MAX_FREQ_KHZ_VAL" =~ ^[0-9]+$ ]] && MAX_FREQ_MHZ=$(( MAX_FREQ_KHZ_VAL / 1000 ))

DETAILS+=(
    "tool=${TOOL}"
    "peak_temp_c=${PEAK_TEMP}"
    "max_freq_mhz=${MAX_FREQ_MHZ}"
    "min_freq_mhz_under_load=${MIN_FREQ_MHZ}"
    "avg_util_pct=${AVG_UTIL}"
    "throttle_sample_count=${THROTTLE_COUNT}"
)

# ── FAIL 판정 ─────────────────────────────────────────────
if [[ $PEAK_TEMP -gt 100 ]]; then
    STATUS="fail"
    DETAILS+=("FAIL:peak_temp_over_100c(${PEAK_TEMP}c)")
fi

# ── WARN 판정 ─────────────────────────────────────────────
if [[ $THROTTLE_COUNT -gt 0 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:freq_throttle_detected_${THROTTLE_COUNT}_samples")
fi
if [[ "$TOOL" != "none" && $AVG_UTIL -lt 80 && $SAMPLE_COUNT -gt 0 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:low_cpu_utilization_avg=${AVG_UTIL}pct")
fi
if [[ $STRESS_DIED -eq 1 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:stress_tool_exited_early")
fi

DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"

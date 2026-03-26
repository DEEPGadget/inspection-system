#!/bin/bash
# sw_power_mgmt.sh — 전원 관리 설정 검사
# FAIL: sleep.target이 masked 아님, CPU governor가 performance 아님
# 출력: {"check":"sw_power_mgmt","status":"pass|fail|warn","detail":"..."}
set -euo pipefail

CHECK="sw_power_mgmt"
STATUS="pass"
DETAILS=()

# ── sleep.target masked 확인 (FAIL 조건) ────────────────
SLEEP_STATE=$(systemctl is-enabled sleep.target 2>/dev/null || echo "unknown")
DETAILS+=("sleep_target=${SLEEP_STATE}")

if [[ "$SLEEP_STATE" != "masked" ]]; then
    STATUS="fail"
    DETAILS+=("FAIL:sleep_target_not_masked(current=${SLEEP_STATE})")
fi

# ── suspend/hibernate 타깃도 확인 ───────────────────────
for target in suspend.target hibernate.target hybrid-sleep.target; do
    STATE=$(systemctl is-enabled "$target" 2>/dev/null || echo "unknown")
    DETAILS+=("${target%.*}=${STATE}")
    if [[ "$STATE" != "masked" && "$STATE" != "disabled" ]]; then
        [[ "$STATUS" == "pass" ]] && STATUS="warn"
        DETAILS+=("WARN:${target}_not_masked")
    fi
done

# ── CPU 주파수 거버너 ────────────────────────────────────
GOV_PATH="/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
if [[ -r "$GOV_PATH" ]]; then
    GOVERNOR=$(cat "$GOV_PATH")
    DETAILS+=("cpu_governor=${GOVERNOR}")
    if [[ "$GOVERNOR" != "performance" ]]; then
        STATUS="fail"
        DETAILS+=("FAIL:cpu_governor_not_performance(current=${GOVERNOR})")
    fi

    # 모든 코어 일관성 확인
    NON_PERF=$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null \
        | grep -cv "^performance$" || true)
    NON_PERF="${NON_PERF:-0}"
    if [[ "$NON_PERF" -gt 0 ]]; then
        STATUS="fail"
        DETAILS+=("FAIL:${NON_PERF}_cores_not_performance_governor")
    fi
else
    DETAILS+=("cpu_governor=unavailable(no_cpufreq)")
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
fi

# ── BIOS/UEFI C-state 제한 확인 ─────────────────────────
# intel_idle 드라이버: max_cstate 값 (0이 최적)
MAX_CSTATE_FILE="/sys/module/intel_idle/parameters/max_cstate"
if [[ -r "$MAX_CSTATE_FILE" ]]; then
    MAX_CSTATE=$(cat "$MAX_CSTATE_FILE")
    DETAILS+=("intel_idle_max_cstate=${MAX_CSTATE}")
    if [[ "$MAX_CSTATE" -gt 1 ]]; then
        [[ "$STATUS" == "pass" ]] && STATUS="warn"
        DETAILS+=("WARN:intel_cstate_above_1")
    fi
fi

# ── Turbo Boost (Intel) ──────────────────────────────────
TURBO_FILE="/sys/devices/system/cpu/intel_pstate/no_turbo"
if [[ -r "$TURBO_FILE" ]]; then
    NO_TURBO=$(cat "$TURBO_FILE")
    DETAILS+=("turbo_boost=$( [[ "$NO_TURBO" == "0" ]] && echo "enabled" || echo "disabled" )")
fi

# ── 현재 전원 프로파일 (tuned) ───────────────────────────
if command -v tuned-adm &>/dev/null; then
    TUNED=$(tuned-adm active 2>/dev/null | grep -oP "Current active profile: \K\S+" || echo "unknown")
    DETAILS+=("tuned_profile=${TUNED}")
    if [[ "$TUNED" != "throughput-performance" && "$TUNED" != "latency-performance" \
          && "$TUNED" != "network-latency" ]]; then
        [[ "$STATUS" == "pass" ]] && STATUS="warn"
        DETAILS+=("WARN:tuned_not_performance_profile")
    fi
fi

DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"

#!/bin/bash
# collect_all_logs.sh — 시스템 로그 및 진단 정보 수집
# NFS 마운트 없이 stdout JSON만 반환. 원시 데이터는 stderr로 출력 (worker가 파일로 저장)
# 출력: {"check":"collect_all_logs","status":"pass|warn","detail":"..."}
set -euo pipefail

CHECK="collect_all_logs"
STATUS="pass"
DETAILS=()
ERRORS=()

# ── dmesg (최근 50줄, 에러/경고만) ──────────────────────
DMESG_ERRS=$(dmesg --level=err,crit,alert,emerg 2>/dev/null | tail -20 | wc -l || true); DMESG_ERRS="${DMESG_ERRS:-0}"
DMESG_WARNS=$(dmesg --level=warn 2>/dev/null | tail -50 | wc -l || true); DMESG_WARNS="${DMESG_WARNS:-0}"
DETAILS+=("dmesg_errors=${DMESG_ERRS}" "dmesg_warnings=${DMESG_WARNS}")

# MCE (Machine Check Exceptions) 확인
MCE_COUNT=$(dmesg 2>/dev/null | grep -ci "mce\|machine.check" || true); MCE_COUNT="${MCE_COUNT:-0}"
DETAILS+=("mce_count=${MCE_COUNT}")
if [[ "$MCE_COUNT" -gt 0 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:mce_events_in_dmesg")
fi

# ── OOM killer 발생 확인 ─────────────────────────────────
OOM_COUNT=$(dmesg 2>/dev/null | grep -c "Out of memory\|oom.killer" || true); OOM_COUNT="${OOM_COUNT:-0}"
DETAILS+=("oom_kill_count=${OOM_COUNT}")
if [[ "$OOM_COUNT" -gt 0 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:oom_events=${OOM_COUNT}")
fi

# ── journalctl 에러 (최근 24h) ───────────────────────────
if command -v journalctl &>/dev/null; then
    JOURNAL_ERRS=$(journalctl -p err --since "24h ago" --no-pager -q 2>/dev/null | wc -l || echo "0")
    DETAILS+=("journal_errors_24h=${JOURNAL_ERRS}")
    if [[ "$JOURNAL_ERRS" -gt 100 ]]; then
        [[ "$STATUS" == "pass" ]] && STATUS="warn"
        DETAILS+=("WARN:many_journal_errors=${JOURNAL_ERRS}")
    fi
fi

# ── NVIDIA GPU 이벤트 ────────────────────────────────────
if command -v nvidia-smi &>/dev/null; then
    # XID 에러 (GPU 하드웨어 에러)
    XID_COUNT=$(dmesg 2>/dev/null | grep -c "NVRM.*Xid" || true); XID_COUNT="${XID_COUNT:-0}"
    DETAILS+=("gpu_xid_errors=${XID_COUNT}")
    if [[ "$XID_COUNT" -gt 0 ]]; then
        [[ "$STATUS" == "pass" ]] && STATUS="warn"
        DETAILS+=("WARN:gpu_xid_events=${XID_COUNT}")
        # XID 번호 추출 (중복 제거)
        XID_NUMS=$(dmesg 2>/dev/null | grep -oP "Xid \(\K[^)]*" | sort -u | tr '\n' ',' | sed 's/,$//')
        DETAILS+=("xid_numbers=${XID_NUMS}")
    fi

    # GPU 리셋 이벤트
    GPU_RESET=$(dmesg 2>/dev/null | grep -c "GPU.reset\|reset GPU" || true); GPU_RESET="${GPU_RESET:-0}"
    [[ "$GPU_RESET" -gt 0 ]] && DETAILS+=("WARN:gpu_reset_events=${GPU_RESET}")
fi

# ── 시스템 부팅 이력 ─────────────────────────────────────
BOOT_COUNT=$(last reboot 2>/dev/null | grep -c "^reboot" || true); BOOT_COUNT="${BOOT_COUNT:-?}"
LAST_REBOOT=$(last reboot 2>/dev/null | head -1 | awk '{print $5,$6,$7,$8}' || echo "unknown")
DETAILS+=("reboot_count=${BOOT_COUNT}" "last_reboot=${LAST_REBOOT// /_}")

# ── 열린 파일 디스크립터 ─────────────────────────────────
FD_MAX=$(cat /proc/sys/fs/file-max 2>/dev/null || echo "?")
FD_USED=$(cat /proc/sys/fs/file-nr 2>/dev/null | awk '{print $1}' || echo "?")
DETAILS+=("fd_max=${FD_MAX}" "fd_used=${FD_USED}")

# ── 로드 애버리지 ────────────────────────────────────────
LOAD=$(cat /proc/loadavg 2>/dev/null | awk '{print $1","$2","$3}')
CPU_COUNT=$(nproc 2>/dev/null || echo "1")
LOAD_1MIN=$(echo "$LOAD" | cut -d, -f1)
LOAD_RATIO=$(awk "BEGIN {printf \"%.1f\", $LOAD_1MIN / $CPU_COUNT}")
DETAILS+=("loadavg_1_5_15=${LOAD}" "load_ratio=${LOAD_RATIO}")

if awk "BEGIN {exit ($LOAD_RATIO < 0.9) ? 0 : 1}"; then
    : # 정상
else
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:high_load_ratio=${LOAD_RATIO}")
fi

DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"

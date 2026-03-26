#!/bin/bash
# sw_storage.sh — 스토리지 장치 목록, 용량, 마운트, 헬스 확인
# 출력: {"check":"sw_storage","status":"pass|fail|warn","detail":"..."}
set -euo pipefail

CHECK="sw_storage"
STATUS="pass"
DETAILS=()

# ── 블록 장치 목록 ───────────────────────────────────────
if ! command -v lsblk &>/dev/null; then
    printf '{"check":"%s","status":"warn","detail":"lsblk not found"}\n' "$CHECK"
    exit 0
fi

# 물리 디스크만 (루프, rom 제외)
DISK_LIST=$(lsblk -dno NAME,SIZE,TYPE,ROTA 2>/dev/null \
    | awk '$3 == "disk" {print $1":"$2":"($4=="0"?"nvme/ssd":"hdd")}' \
    | tr '\n' ',' | sed 's/,$//')
DISK_COUNT=$(lsblk -dno NAME,TYPE 2>/dev/null | awk '$2=="disk"' | wc -l)

DETAILS+=("disk_count=${DISK_COUNT}" "disks=${DISK_LIST}")

# ── 루트 파일시스템 사용률 ───────────────────────────────
ROOT_USE=$(df -h / 2>/dev/null | awk 'NR==2{print $5}' | tr -d '%')
ROOT_AVAIL=$(df -h / 2>/dev/null | awk 'NR==2{print $4}')
DETAILS+=("root_use_pct=${ROOT_USE}" "root_avail=${ROOT_AVAIL}")
if [[ "${ROOT_USE:-0}" -gt 90 ]]; then
    STATUS="fail"
    DETAILS+=("FAIL:root_disk_over_90pct")
elif [[ "${ROOT_USE:-0}" -gt 80 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:root_disk_over_80pct")
fi

# ── NVMe 헬스 (nvme-cli) ────────────────────────────────
if command -v nvme &>/dev/null; then
    for dev in $(lsblk -dno NAME,TYPE 2>/dev/null | awk '$2=="disk"{print $1}' | grep "^nvme"); do
        SMART_RAW=$(nvme smart-log "/dev/${dev}" 2>/dev/null || true)
        if [[ -n "$SMART_RAW" ]]; then
            CRITICAL=$(echo "$SMART_RAW" | grep -oP "critical_warning\s*:\s*\K\d+" || echo "0")
            AVAIL=$(echo "$SMART_RAW" | grep -oP "avail_spare\s*:\s*\K\d+" || echo "100")
            WEAR=$(echo "$SMART_RAW" | grep -oP "percent_used\s*:\s*\K\d+" || echo "0")
            DETAILS+=("nvme_${dev}_critical=${CRITICAL}" \
                      "nvme_${dev}_avail_spare_pct=${AVAIL}" \
                      "nvme_${dev}_wear_pct=${WEAR}")
            if [[ "$CRITICAL" -ne 0 || "${AVAIL:-100}" -lt 10 ]]; then
                STATUS="fail"
                DETAILS+=("FAIL:nvme_${dev}_health_critical")
            elif [[ "${WEAR:-0}" -gt 80 ]]; then
                [[ "$STATUS" == "pass" ]] && STATUS="warn"
                DETAILS+=("WARN:nvme_${dev}_wear_high")
            fi
        fi
    done
else
    DETAILS+=("nvme_cli=not_installed")
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
fi

# ── SATA/SAS SMART (smartctl) ───────────────────────────
if command -v smartctl &>/dev/null; then
    for dev in $(lsblk -dno NAME,TYPE 2>/dev/null | awk '$2=="disk"{print $1}' | grep -v "^nvme"); do
        HEALTH=$(smartctl -H "/dev/${dev}" 2>/dev/null | grep -oP "SMART overall-health.*:\s+\K\w+" || echo "UNKNOWN")
        DETAILS+=("smart_${dev}=${HEALTH}")
        if [[ "$HEALTH" == "FAILED" ]]; then
            STATUS="fail"
            DETAILS+=("FAIL:smart_${dev}_failed")
        elif [[ "$HEALTH" != "PASSED" && "$HEALTH" != "UNKNOWN" ]]; then
            [[ "$STATUS" == "pass" ]] && STATUS="warn"
        fi
    done
fi

# ── RAID/MD 상태 ─────────────────────────────────────────
if [[ -f /proc/mdstat ]]; then
    MD_DEGRADED=$(grep -c "\[.*_.*\]" /proc/mdstat 2>/dev/null || true)
    MD_DEGRADED="${MD_DEGRADED:-0}"
    if [[ "$MD_DEGRADED" -gt 0 ]]; then
        STATUS="fail"
        DETAILS+=("FAIL:md_raid_degraded=${MD_DEGRADED}")
    fi
fi

DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"

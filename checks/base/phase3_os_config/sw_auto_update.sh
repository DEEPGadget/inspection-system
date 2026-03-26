#!/bin/bash
# sw_auto_update.sh — 자동 업데이트 비활성화 확인
# FAIL: unattended-upgrades 또는 dnf-automatic이 enabled/active 상태
# 출력: {"check":"sw_auto_update","status":"pass|fail|warn","detail":"..."}
set -euo pipefail

CHECK="sw_auto_update"
STATUS="pass"
DETAILS=()

# ── OS 계열 판별 ─────────────────────────────────────────
if [[ -f /etc/os-release ]]; then
    OS_ID=$(grep "^ID=" /etc/os-release | cut -d= -f2 | tr -d '"')
    OS_ID_LIKE=$(grep "^ID_LIKE=" /etc/os-release | cut -d= -f2 | tr -d '"' || echo "")
else
    OS_ID="unknown"
    OS_ID_LIKE=""
fi
DETAILS+=("os=${OS_ID}")

# ── Ubuntu / Debian: unattended-upgrades ────────────────
if [[ "$OS_ID" == "ubuntu" || "$OS_ID" == "debian" || "$OS_ID_LIKE" == *"debian"* ]]; then
    UU_ENABLED=$(systemctl is-enabled unattended-upgrades 2>/dev/null || echo "not-found")
    UU_ACTIVE=$(systemctl is-active  unattended-upgrades 2>/dev/null || echo "inactive")
    DETAILS+=("unattended_upgrades_enabled=${UU_ENABLED}" "unattended_upgrades_active=${UU_ACTIVE}")

    if [[ "$UU_ENABLED" == "enabled" || "$UU_ACTIVE" == "active" ]]; then
        STATUS="fail"
        DETAILS+=("FAIL:unattended_upgrades_running")
    fi

    # APT 자동 업데이트 타이머
    APT_TIMER=$(systemctl is-enabled apt-daily-upgrade.timer 2>/dev/null || echo "not-found")
    DETAILS+=("apt_daily_upgrade_timer=${APT_TIMER}")
    if [[ "$APT_TIMER" == "enabled" ]]; then
        [[ "$STATUS" == "pass" ]] && STATUS="warn"
        DETAILS+=("WARN:apt_daily_upgrade_timer_enabled")
    fi

    # /etc/apt/apt.conf.d/20auto-upgrades 설정 확인
    AUTO_CFG="/etc/apt/apt.conf.d/20auto-upgrades"
    if [[ -f "$AUTO_CFG" ]]; then
        PERIODIC=$(grep -oP 'APT::Periodic::Unattended-Upgrade\s+"\K\d+' "$AUTO_CFG" || echo "0")
        DETAILS+=("apt_periodic_unattended=${PERIODIC}")
        if [[ "${PERIODIC:-0}" -ne 0 ]]; then
            [[ "$STATUS" == "pass" ]] && STATUS="warn"
            DETAILS+=("WARN:apt_periodic_unattended_nonzero")
        fi
    fi
fi

# ── RHEL / CentOS / Rocky: dnf-automatic ────────────────
if [[ "$OS_ID" =~ ^(rhel|centos|rocky|almalinux|fedora)$ || "$OS_ID_LIKE" == *"rhel"* ]]; then
    DNF_AUTO=$(systemctl is-enabled dnf-automatic.timer 2>/dev/null \
        || systemctl is-enabled dnf-automatic 2>/dev/null \
        || echo "not-found")
    DETAILS+=("dnf_automatic=${DNF_AUTO}")
    if [[ "$DNF_AUTO" == "enabled" ]]; then
        STATUS="fail"
        DETAILS+=("FAIL:dnf_automatic_enabled")
    fi

    # yum-cron (CentOS 7 이하)
    YUM_CRON=$(systemctl is-enabled yum-cron 2>/dev/null || echo "not-found")
    DETAILS+=("yum_cron=${YUM_CRON}")
    if [[ "$YUM_CRON" == "enabled" ]]; then
        STATUS="fail"
        DETAILS+=("FAIL:yum_cron_enabled")
    fi
fi

# ── snapd 자동 새로고침 (Ubuntu) ────────────────────────
if command -v snap &>/dev/null; then
    SNAP_HOLD=$(snap get system refresh.hold 2>/dev/null || echo "none")
    DETAILS+=("snap_refresh_hold=${SNAP_HOLD}")
fi

DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"

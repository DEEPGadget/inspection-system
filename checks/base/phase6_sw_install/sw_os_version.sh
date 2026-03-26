#!/bin/bash
# sw_os_version.sh — OS 버전, 커널, 필수 패키지 확인
# 출력: {"check":"sw_os_version","status":"pass|fail|warn","detail":"..."}
set -euo pipefail

CHECK="sw_os_version"
STATUS="pass"
DETAILS=()

# ── OS 정보 ──────────────────────────────────────────────
if [[ -f /etc/os-release ]]; then
    OS_NAME=$(grep "^NAME="    /etc/os-release | cut -d= -f2 | tr -d '"')
    OS_VER=$(grep  "^VERSION=" /etc/os-release | cut -d= -f2 | tr -d '"' || \
             grep "^VERSION_ID=" /etc/os-release | cut -d= -f2 | tr -d '"')
    OS_ID=$(grep   "^ID="      /etc/os-release | cut -d= -f2 | tr -d '"')
    DETAILS+=("os_name=${OS_NAME}" "os_version=${OS_VER}" "os_id=${OS_ID}")
else
    DETAILS+=("os_release=not_found")
    STATUS="warn"
fi

# ── 커널 버전 ────────────────────────────────────────────
KERNEL=$(uname -r)
ARCH=$(uname -m)
DETAILS+=("kernel=${KERNEL}" "arch=${ARCH}")

# 커널 버전이 너무 오래된 경우 warn (5.4 미만)
KERNEL_MAJOR=$(echo "$KERNEL" | cut -d. -f1)
KERNEL_MINOR=$(echo "$KERNEL" | cut -d. -f2)
if [[ "$KERNEL_MAJOR" -lt 5 ]] || [[ "$KERNEL_MAJOR" -eq 5 && "$KERNEL_MINOR" -lt 4 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:kernel_below_5_4")
fi

# ── 지원 OS 확인 ─────────────────────────────────────────
# GPU 서버 공식 지원: Ubuntu 20.04/22.04, RHEL 8/9
SUPPORTED=0
case "$OS_ID" in
    ubuntu)
        VER_ID=$(grep "^VERSION_ID=" /etc/os-release | cut -d= -f2 | tr -d '"')
        [[ "$VER_ID" == "20.04" || "$VER_ID" == "22.04" || "$VER_ID" == "24.04" ]] && SUPPORTED=1
        ;;
    rhel|rocky|almalinux|centos)
        VER_ID=$(grep "^VERSION_ID=" /etc/os-release | cut -d= -f2 | tr -d '"' | cut -d. -f1)
        [[ "$VER_ID" == "8" || "$VER_ID" == "9" ]] && SUPPORTED=1
        ;;
    *) SUPPORTED=0 ;;
esac
DETAILS+=("supported_os=$( [[ $SUPPORTED -eq 1 ]] && echo "yes" || echo "no" )")
if [[ $SUPPORTED -eq 0 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:os_not_in_supported_list")
fi

# ── 필수 패키지 존재 확인 ────────────────────────────────
REQUIRED_PKGS=("bash" "curl" "jq" "lsblk" "ip")
MISSING=()
for pkg in "${REQUIRED_PKGS[@]}"; do
    if ! command -v "$pkg" &>/dev/null; then
        MISSING+=("$pkg")
    fi
done
if [[ "${#MISSING[@]}" -gt 0 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:missing_tools=$(IFS=','; echo "${MISSING[*]}")")
else
    DETAILS+=("required_tools=ok")
fi

# ── 시스템 업타임 ────────────────────────────────────────
UPTIME_SEC=$(awk '{print int($1)}' /proc/uptime 2>/dev/null || echo "?")
if [[ "$UPTIME_SEC" != "?" ]]; then
    UPTIME_DAYS=$(( UPTIME_SEC / 86400 ))
    DETAILS+=("uptime_days=${UPTIME_DAYS}")
fi

# ── 시간 동기화 (chronyd / ntpd) ─────────────────────────
if command -v chronyc &>/dev/null; then
    SYNC=$(chronyc tracking 2>/dev/null | grep -oP "System time\s+:\s+\K[\d.]+" || echo "?")
    DETAILS+=("chrony_offset_sec=${SYNC}")
elif command -v timedatectl &>/dev/null; then
    NTP_SYNC=$(timedatectl show 2>/dev/null | grep "NTPSynchronized" | cut -d= -f2 || echo "?")
    DETAILS+=("ntp_synchronized=${NTP_SYNC}")
fi

DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"

#!/bin/bash
# sw_network.sh — NIC 목록, 링크 속도, 연결 상태 확인
# 출력: {"check":"sw_network","status":"pass|fail|warn","detail":"..."}
set -euo pipefail

CHECK="sw_network"
STATUS="pass"
DETAILS=()

# ── NIC 목록 (루프백·가상 제외) ─────────────────────────
NIC_UP=()
NIC_DOWN=()
ALL_NICS=()

while IFS= read -r iface; do
    [[ "$iface" == "lo" ]] && continue
    # 가상 인터페이스 제외 (docker, veth, virbr 등)
    [[ "$iface" =~ ^(docker|veth|virbr|br-|tun|tap) ]] && continue

    OPERSTATE=$(cat "/sys/class/net/${iface}/operstate" 2>/dev/null || echo "unknown")
    SPEED_FILE="/sys/class/net/${iface}/speed"
    SPEED="?"
    if [[ -r "$SPEED_FILE" ]]; then
        RAW_SPEED=$(cat "$SPEED_FILE" 2>/dev/null || echo "-1")
        if [[ "$RAW_SPEED" -gt 0 ]]; then
            if [[ "$RAW_SPEED" -ge 1000000 ]]; then
                SPEED="$(awk "BEGIN {printf \"%dTb\", $RAW_SPEED/1000000}")E"
            elif [[ "$RAW_SPEED" -ge 1000 ]]; then
                SPEED="$(awk "BEGIN {printf \"%dGb\", $RAW_SPEED/1000}")E"
            else
                SPEED="${RAW_SPEED}Mb"
            fi
        fi
    fi

    ALL_NICS+=("${iface}(${SPEED},${OPERSTATE})")

    if [[ "$OPERSTATE" == "up" ]]; then
        NIC_UP+=("$iface")
    else
        NIC_DOWN+=("$iface")
    fi
done < <(ls /sys/class/net/ 2>/dev/null)

DETAILS+=("nics=$(IFS=','; echo "${ALL_NICS[*]:-none}")")
DETAILS+=("up_count=${#NIC_UP[@]}" "down_count=${#NIC_DOWN[@]}")

if [[ "${#NIC_UP[@]}" -eq 0 ]]; then
    STATUS="fail"
    DETAILS+=("FAIL:no_active_nic")
elif [[ "${#NIC_DOWN[@]}" -gt 0 ]]; then
    [[ "$STATUS" == "pass" ]] && STATUS="warn"
    DETAILS+=("WARN:nics_down=$(IFS=','; echo "${NIC_DOWN[*]}")")
fi

# ── InfiniBand / RDMA (선택) ────────────────────────────
if command -v ibstat &>/dev/null; then
    IB_ACTIVE=$(ibstat 2>/dev/null | grep -c "State: Active" || true); IB_ACTIVE="${IB_ACTIVE:-0}"
    IB_PORTS=$(ibstat 2>/dev/null | grep -c "^Port" || true); IB_PORTS="${IB_PORTS:-0}"
    DETAILS+=("ib_ports=${IB_PORTS}" "ib_active=${IB_ACTIVE}")
    if [[ "$IB_PORTS" -gt 0 && "$IB_ACTIVE" -eq 0 ]]; then
        [[ "$STATUS" == "pass" ]] && STATUS="warn"
        DETAILS+=("WARN:ib_ports_not_active")
    fi
fi

# ── 로컬 루프백 연결 확인 ────────────────────────────────
if ping -c1 -W1 127.0.0.1 &>/dev/null; then
    DETAILS+=("loopback=ok")
else
    STATUS="fail"
    DETAILS+=("FAIL:loopback_ping_failed")
fi

# ── MTU 확인 (점보 프레임 여부) ─────────────────────────
for iface in "${NIC_UP[@]}"; do
    MTU=$(cat "/sys/class/net/${iface}/mtu" 2>/dev/null || echo "?")
    DETAILS+=("${iface}_mtu=${MTU}")
done

DETAIL_STR=$(IFS="|"; echo "${DETAILS[*]}")
printf '{"check":"%s","status":"%s","detail":"%s"}\n' "$CHECK" "$STATUS" "$DETAIL_STR"

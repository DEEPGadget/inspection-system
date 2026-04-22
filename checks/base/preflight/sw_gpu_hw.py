#!/usr/bin/env python3
"""
GPU H/W 검수 — driver 없이 lspci로 GPU 존재·수량·PCIe width·speed 확인.
post_install 단계의 sw_gpu_sw.py와 역할 분리:
  이 스크립트: H/W 관점 (driver 불필요)
  sw_gpu_sw.py: S/W 관점 (nvidia-smi, driver 필요)

환경변수:
  SUDO_PASSWORD  lspci -vv가 LnkSta/LnkCap 읽으려면 sudo 필요

출력 필드:
  gpu_count       감지된 NVIDIA GPU 수
  link_width      LnkCap Width (하드웨어 최대, 모든 GPU 동일하면 단일값 / 다르면 mixed)
  link_speed      LnkCap Speed (하드웨어 최대)
  link_state      현재 실측: "Speed Gen/WidthxN" 요약 (idle 시 downgrade 가능)
  downgraded_gpus LnkSta가 LnkCap보다 낮은 GPU index 목록 ("none" 또는 "0,3")
"""

import json
import os
import re
import subprocess
import sys


def _run(cmd: str, timeout: int = 10) -> str:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def get_gpu_pci_entries() -> list[dict]:
    """lspci로 NVIDIA GPU PCI 엔트리 목록 반환."""
    output = _run("lspci -mm -d 10de:")
    if not output:
        return []

    entries = []
    for line in output.splitlines():
        parts = re.findall(r'"([^"]*)"', line)
        if len(parts) < 3:
            continue
        pci_class = parts[0]
        if pci_class not in ("VGA compatible controller", "3D controller"):
            continue
        slot = line.split()[0] if line else ""
        device = parts[2] if len(parts) > 2 else "unknown"
        entries.append({"slot": slot, "device": device})
    return entries


def get_pcie_info(slot: str, sudo_pw: str | None) -> dict:
    """lspci -vv로 PCIe LnkCap + LnkSta 반환.

    lspci -vv는 sudo 없으면 LnkSta/LnkCap 안 나옴. sudo_pw 있으면 sudo 경유.
    """
    if not slot:
        return {}
    if sudo_pw:
        # -S로 stdin password, 2>/dev/null 로 sudo 프롬프트 에러 감춤
        cmd = f"echo {sudo_pw} | sudo -S lspci -vv -s {slot} 2>/dev/null"
    else:
        cmd = f"lspci -vv -s {slot} 2>/dev/null"
    output = _run(cmd)

    info: dict = {}
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("LnkCap:"):
            m_speed = re.search(r"Speed\s+([\d.]+GT/s)", stripped)
            m_width = re.search(r"Width\s+(x\d+)", stripped)
            if m_speed:
                info["cap_speed"] = m_speed.group(1)
            if m_width:
                info["cap_width"] = m_width.group(1)
        elif stripped.startswith("LnkSta:"):
            m_speed = re.search(r"Speed\s+([\d.]+GT/s)", stripped)
            m_width = re.search(r"Width\s+(x\d+)", stripped)
            if m_speed:
                info["state_speed"] = m_speed.group(1)
            if m_width:
                info["state_width"] = m_width.group(1)
            info["downgraded"] = "downgraded" in stripped
    return info


def _summarize(values: list[str]) -> str:
    """단일값이면 그대로, 다르면 'mixed(a,b)' 포맷."""
    uniq = sorted(set(v for v in values if v and v != "unknown"))
    if not uniq:
        return "unknown"
    if len(uniq) == 1:
        return uniq[0]
    return "mixed(" + ",".join(uniq) + ")"


def main():
    try:
        sudo_pw = os.environ.get("SUDO_PASSWORD") or None
        entries = get_gpu_pci_entries()
        gpu_count = len(entries)

        if gpu_count == 0:
            print(json.dumps({"check": "sw_gpu_hw", "status": "fail", "detail": "gpu_count=0"}))
            return

        # 모든 GPU 링크 정보 수집
        cap_widths: list[str] = []
        cap_speeds: list[str] = []
        state_widths: list[str] = []
        state_speeds: list[str] = []
        downgraded_idx: list[str] = []

        for i, entry in enumerate(entries):
            info = get_pcie_info(entry["slot"], sudo_pw)
            cap_widths.append(info.get("cap_width", "unknown"))
            cap_speeds.append(info.get("cap_speed", "unknown"))
            state_widths.append(info.get("state_width", "unknown"))
            state_speeds.append(info.get("state_speed", "unknown"))
            if info.get("downgraded"):
                downgraded_idx.append(str(i))

        link_width = _summarize(cap_widths)
        link_speed = _summarize(cap_speeds)
        state_w = _summarize(state_widths)
        state_s = _summarize(state_speeds)

        # 판정: LnkCap Width가 x16 미만이면 warn (HW 스펙 부족)
        # LnkSta downgrade는 idle 시 흔한 경우라 별도 처리 안 함 (부하 중이면 stress_gpu에서 확인)
        status = "pass"
        if link_width not in ("x16", "unknown") and not link_width.startswith("mixed"):
            try:
                width_val = int(link_width.lstrip("x"))
                if width_val < 16:
                    status = "warn"
            except ValueError:
                pass

        # lspci -vv에 sudo 권한 없어서 전부 unknown이면 경고 (동작 점검용)
        lspci_unauthorized = all(cw == "unknown" for cw in cap_widths)
        if lspci_unauthorized:
            status = "warn" if status == "pass" else status

        detail_parts = [
            f"gpu_count={gpu_count}",
            f"link_width={link_width}",
            f"link_speed={link_speed}",
            f"link_state_width={state_w}",
            f"link_state_speed={state_s}",
            f"downgraded_gpus={','.join(downgraded_idx) if downgraded_idx else 'none'}",
        ]
        if lspci_unauthorized:
            detail_parts.append("WARN:lspci_vv_requires_sudo")

        print(
            json.dumps(
                {
                    "check": "sw_gpu_hw",
                    "status": status,
                    "detail": "|".join(detail_parts),
                }
            )
        )

    except Exception as e:
        print(f"Error in sw_gpu_hw: {e}", file=sys.stderr)
        print(json.dumps({"check": "sw_gpu_hw", "status": "fail", "detail": "error=exception"}))


if __name__ == "__main__":
    main()

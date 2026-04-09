#!/usr/bin/env python3
"""
GPU H/W 검수 — driver 없이 lspci로 GPU 존재·수량·PCIe width·speed 확인.
post_install 단계의 sw_gpu_sw.py와 역할 분리:
  이 스크립트: H/W 관점 (driver 불필요)
  sw_gpu_sw.py: S/W 관점 (nvidia-smi, driver 필요)
"""

import json
import re
import subprocess
import sys


def _run(cmd: str, timeout: int = 10) -> str:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def get_gpu_pci_entries() -> list[dict]:
    """lspci로 NVIDIA GPU PCI 엔트리 목록 반환.
    vendor 10de (NVIDIA) 중 VGA/3D controller 클래스만 포함.
    Audio device(HDMI audio)는 제외 — RTX 계열은 GPU당 Audio 엔트리가 별도 존재.
    """
    # -d 10de: vendor filter, 클래스는 수동 grep (lspci는 class ID로도 필터 가능하나
    # VGA=0300, 3D=0302 로 나뉘어 있어 문자열 매칭이 단순)
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


def get_pcie_info(slot: str) -> dict:
    """lspci -vv로 PCIe link width/speed 확인."""
    if not slot:
        return {}
    output = _run(f"lspci -vv -s {slot} 2>/dev/null")
    info = {}
    for line in output.splitlines():
        line = line.strip()
        if "LnkSta:" in line:
            # LnkSta: Speed 16GT/s (ok), Width x16 (ok)
            speed_m = re.search(r"Speed\s+([\d.]+GT/s)", line)
            width_m = re.search(r"Width\s+(x\d+)", line)
            if speed_m:
                info["link_speed"] = speed_m.group(1)
            if width_m:
                info["link_width"] = width_m.group(1)
            break
    return info


def main():
    try:
        entries = get_gpu_pci_entries()
        gpu_count = len(entries)

        if gpu_count == 0:
            print(json.dumps({"check": "sw_gpu_hw", "status": "fail", "detail": "gpu_count=0"}))
            return

        # PCIe 정보 수집 (첫 번째 GPU 기준)
        pcie = get_pcie_info(entries[0]["slot"]) if entries else {}
        link_speed = pcie.get("link_speed", "unknown")
        link_width = pcie.get("link_width", "unknown")

        # width x16 미만이면 warn
        status = "pass"
        if link_width not in ("x16", "unknown"):
            try:
                width_val = int(link_width.lstrip("x"))
                if width_val < 16:
                    status = "warn"
            except ValueError:
                pass

        detail_parts = [
            f"gpu_count={gpu_count}",
            f"link_speed={link_speed}",
            f"link_width={link_width}",
        ]

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

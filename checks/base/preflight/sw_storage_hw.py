#!/usr/bin/env python3
"""
스토리지 H/W 검수 — lsblk로 디스크 목록·용량·RAID 확인.
driver/nvme-cli 불필요. post_install 단계의 sw_storage_sw.py와 역할 분리:
  이 스크립트: H/W 관점 (lsblk, RAID topology)
  sw_storage_sw.py: S/W 관점 (nvme-cli SMART, 헬스)
"""

import json
import re
import subprocess
import sys


def _run(cmd: str, timeout: int = 10) -> str:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def get_block_devices() -> list[dict]:
    output = _run("lsblk -dno NAME,SIZE,TYPE,ROTA")
    devices = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "disk":
            name, size, _, rota = parts[0], parts[1], parts[2], parts[3]
            if rota == "0":
                dev_type = "nvme" if name.startswith("nvme") else "ssd"
            else:
                dev_type = "hdd"
            devices.append({"name": name, "size": size, "type": dev_type})
    return devices


def check_md_raid_degraded() -> int:
    try:
        with open("/proc/mdstat") as f:
            content = f.read()
        return sum(1 for line in content.splitlines() if re.search(r"\[.*_.*\]", line))
    except Exception:
        return 0


def main():
    try:
        devices = get_block_devices()
        md_degraded = check_md_raid_degraded()

        status = "pass"
        if not devices:
            status = "fail"
        elif md_degraded > 0:
            status = "fail"

        device_summary = (
            "|".join(f"{d['name']}({d['type']},{d['size']})" for d in devices)
            if devices
            else "none"
        )

        detail = "|".join(
            [
                f"disk_count={len(devices)}",
                f"devices={device_summary}",
                f"md_degraded={md_degraded}",
            ]
        )

        print(json.dumps({"check": "sw_storage_hw", "status": status, "detail": detail}))

    except Exception as e:
        print(f"Error in sw_storage_hw: {e}", file=sys.stderr)
        print(json.dumps({"check": "sw_storage_hw", "status": "fail", "detail": "error=exception"}))


if __name__ == "__main__":
    main()

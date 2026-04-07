#!/usr/bin/env python3
"""
스토리지 S/W 검수 — nvme-cli SMART·헬스, smartctl(SATA/SAS), 루트 파티션 사용률 확인.
preflight 단계의 sw_storage_hw.py와 역할 분리:
  sw_storage_hw.py: H/W 관점 (lsblk)
  이 스크립트: S/W 관점 (nvme-cli SMART, smartctl, df)
"""

import json
import os
import re
import subprocess
import sys


def _run(cmd: str, timeout: int = 10, stdin_data: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin_data or None,
    )


def get_nvme_devices() -> list[str]:
    result = _run("lsblk -dno NAME,TYPE | awk '$2==\"disk\" && $1~/^nvme/ {print $1}'")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def get_non_nvme_disks() -> list[str]:
    """NVMe 제외한 디스크 목록 (SATA/SAS SSD·HDD)."""
    result = _run("lsblk -dno NAME,TYPE | awk '$2==\"disk\" && $1!~/^nvme/ {print $1}'")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def check_nvme_smart(device: str) -> dict | None:
    result = _run(f"nvme smart-log /dev/{device}")
    if result.returncode != 0:
        return None
    info: dict = {}
    for line in result.stdout.splitlines():
        if "critical_warning" in line.lower():
            m = re.search(r":\s*(\d+)", line)
            if m:
                info["critical_warning"] = int(m.group(1))
        elif "avail_spare" in line.lower() and "threshold" not in line.lower():
            m = re.search(r":\s*(\d+)", line)
            if m:
                info["avail_spare"] = int(m.group(1))
        elif "percent_used" in line.lower():
            m = re.search(r":\s*(\d+)", line)
            if m:
                info["percent_used"] = int(m.group(1))
    return info


def check_smartctl_health(device: str, sudo_password: str) -> str | None:
    """SATA/SAS 디스크 smartctl 헬스 체크. 'PASSED'/'FAILED'/None(미지원) 반환."""
    result = _run(
        f"sudo -S smartctl -H /dev/{device} 2>/dev/null",
        timeout=30,
        stdin_data=sudo_password + "\n" if sudo_password else "",
    )
    if result.returncode not in (0, 4):
        # 0=정상, 4=SMART 경고 있음. 그 외는 smartctl 미설치 등
        return None
    for line in result.stdout.splitlines():
        if "SMART overall-health self-assessment test result:" in line:
            return "PASSED" if "PASSED" in line else "FAILED"
    return None


def check_root_fs() -> tuple[int, str]:
    result = _run("df -h /")
    if result.returncode != 0:
        return 0, "0B"
    lines = result.stdout.strip().splitlines()
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 5:
            try:
                return int(parts[4].rstrip("%")), parts[3]
            except ValueError:
                pass
    return 0, "0B"


def main():
    try:
        sudo_password = os.environ.get("SUDO_PASSWORD", "")
        nvme_devices = get_nvme_devices()
        non_nvme_disks = get_non_nvme_disks()
        root_used_pct, root_avail = check_root_fs()

        nvme_critical_count = 0
        nvme_wear_high_count = 0
        nvme_cli_available = True

        for dev in nvme_devices:
            health = check_nvme_smart(dev)
            if health is None:
                nvme_cli_available = False
                continue
            if health.get("critical_warning", 0) > 0:
                nvme_critical_count += 1
            if health.get("percent_used", 0) > 80:
                nvme_wear_high_count += 1

        sata_failed_count = 0
        sata_checked_count = 0
        for dev in non_nvme_disks:
            result = check_smartctl_health(dev, sudo_password)
            if result is None:
                continue
            sata_checked_count += 1
            if result == "FAILED":
                sata_failed_count += 1

        status = "pass"
        if root_used_pct > 90:
            status = "fail"
        elif nvme_critical_count > 0:
            status = "fail"
        elif sata_failed_count > 0:
            status = "fail"
        elif root_used_pct > 80:
            status = "warn"
        elif nvme_wear_high_count > 0:
            status = "warn"
        elif nvme_devices and not nvme_cli_available:
            status = "warn"

        detail = "|".join(
            [
                f"root_used_pct={root_used_pct}",
                f"root_avail={root_avail}",
                f"nvme_count={len(nvme_devices)}",
                f"nvme_critical={nvme_critical_count}",
                f"nvme_wear_high={nvme_wear_high_count}",
                f"nvme_cli={'available' if nvme_cli_available else 'missing'}",
                f"sata_count={len(non_nvme_disks)}",
                f"sata_checked={sata_checked_count}",
                f"sata_failed={sata_failed_count}",
            ]
        )

        print(json.dumps({"check": "sw_storage_sw", "status": status, "detail": detail}))

    except Exception as e:
        print(f"Error in sw_storage_sw: {e}", file=sys.stderr)
        print(json.dumps({"check": "sw_storage_sw", "status": "fail", "detail": "error=exception"}))


if __name__ == "__main__":
    main()

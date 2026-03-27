#!/usr/bin/env python3

import os
import sys
import json
import subprocess
import re


def get_block_devices():
    """Get block device information using lsblk"""
    try:
        result = subprocess.run(['lsblk', '-dno', 'NAME,SIZE,TYPE,ROTA'],
                              capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return []

        devices = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue

            parts = line.split()
            if len(parts) >= 4 and parts[2] == 'disk':
                name = parts[0]
                size = parts[1]
                rota = parts[3]

                # Determine device type based on rotation
                if rota == '0':
                    if name.startswith('nvme'):
                        dev_type = 'nvme'
                    else:
                        dev_type = 'ssd'
                else:
                    dev_type = 'hdd'

                devices.append({
                    'name': name,
                    'size': size,
                    'type': dev_type
                })

        return devices
    except Exception:
        return []


def check_root_filesystem():
    """Check root filesystem usage"""
    try:
        result = subprocess.run(['df', '-h', '/'], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return 0, "0B"

        lines = result.stdout.strip().split('\n')
        if len(lines) >= 2:
            # Parse the df output
            parts = lines[1].split()
            if len(parts) >= 5:
                used_percent_str = parts[4]  # e.g., "45%"
                available = parts[3]         # e.g., "123G"

                # Extract percentage
                used_percent = int(used_percent_str.rstrip('%'))
                return used_percent, available

        return 0, "0B"
    except Exception:
        return 0, "0B"


def check_nvme_health(device_name):
    """Check NVMe SMART health"""
    try:
        result = subprocess.run(['nvme', 'smart-log', f'/dev/{device_name}'],
                              capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None

        critical_warning = 0
        avail_spare = 100
        percent_used = 0

        for line in result.stdout.split('\n'):
            if 'critical_warning' in line.lower():
                match = re.search(r':\s*(\d+)', line)
                if match:
                    critical_warning = int(match.group(1))
            elif 'avail_spare' in line.lower() and 'threshold' not in line.lower():
                match = re.search(r':\s*(\d+)', line)
                if match:
                    avail_spare = int(match.group(1))
            elif 'percent_used' in line.lower():
                match = re.search(r':\s*(\d+)', line)
                if match:
                    percent_used = int(match.group(1))

        return {
            'critical_warning': critical_warning,
            'avail_spare': avail_spare,
            'percent_used': percent_used
        }
    except Exception:
        return None


def check_smart_health(device_name):
    """Check SMART health for traditional drives"""
    try:
        result = subprocess.run(['smartctl', '-H', f'/dev/{device_name}'],
                              capture_output=True, text=True, timeout=10)
        if result.returncode in [0, 4]:  # 0 = healthy, 4 = some info available
            for line in result.stdout.split('\n'):
                if 'overall-health' in line.lower():
                    if 'PASSED' in line:
                        return 'PASSED'
                    elif 'FAILED' in line:
                        return 'FAILED'
            return 'UNKNOWN'
        return 'UNKNOWN'
    except Exception:
        return 'UNKNOWN'


def check_md_raid():
    """Check MD RAID status"""
    try:
        with open('/proc/mdstat', 'r') as f:
            content = f.read()

        # Look for degraded RAID arrays
        degraded_count = 0
        for line in content.split('\n'):
            # Look for [UU_U] or [U_U] patterns indicating degraded arrays
            if re.search(r'\[.*_.*\]', line):
                degraded_count += 1

        return degraded_count
    except Exception:
        return 0


def main():
    try:
        # Get block devices
        devices = get_block_devices()

        # Check root filesystem
        root_used_percent, root_available = check_root_filesystem()

        # Check device health
        nvme_cli_available = True
        nvme_devices = [dev for dev in devices if dev['type'] == 'nvme']
        ssd_hdd_devices = [dev for dev in devices if dev['type'] in ['ssd', 'hdd']]

        nvme_critical_count = 0
        nvme_wear_high_count = 0
        smart_failed_count = 0

        # Check NVMe devices
        for device in nvme_devices:
            health = check_nvme_health(device['name'])
            if health is None:
                nvme_cli_available = False
                continue

            if health['critical_warning'] > 0:
                nvme_critical_count += 1

            if health['percent_used'] > 80:
                nvme_wear_high_count += 1

        # Check SMART for SSD/HDD
        for device in ssd_hdd_devices:
            health = check_smart_health(device['name'])
            if health == 'FAILED':
                smart_failed_count += 1

        # Check MD RAID
        md_degraded = check_md_raid()

        # Determine status
        status = "pass"
        if root_used_percent > 90:
            status = "fail"
        elif nvme_critical_count > 0:
            status = "fail"
        elif smart_failed_count > 0:
            status = "fail"
        elif md_degraded > 0:
            status = "fail"
        elif root_used_percent > 80:
            status = "warn"
        elif nvme_wear_high_count > 0:
            status = "warn"
        elif nvme_devices and not nvme_cli_available:
            status = "warn"

        # Build device summary
        device_summary = "|".join([f"{dev['name']}({dev['type']},{dev['size']})"
                                  for dev in devices]) if devices else "none"

        # Build detail string
        detail_parts = [
            f"devices={device_summary}",
            f"root_used_pct={root_used_percent}",
            f"root_avail={root_available}",
            f"nvme_critical={nvme_critical_count}",
            f"nvme_wear_high={nvme_wear_high_count}",
            f"smart_failed={smart_failed_count}",
            f"md_degraded={md_degraded}",
            f"nvme_cli={'available' if nvme_cli_available else 'missing'}"
        ]

        detail = "|".join(detail_parts)

        result = {
            "check": "sw_storage",
            "status": status,
            "detail": detail
        }

        print(json.dumps(result))

    except Exception as e:
        # Log error to stderr and output fail status
        print(f"Error in sw_storage check: {e}", file=sys.stderr)
        result = {
            "check": "sw_storage",
            "status": "fail",
            "detail": "error=exception"
        }
        print(json.dumps(result))


if __name__ == "__main__":
    main()
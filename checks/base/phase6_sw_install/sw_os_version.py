#!/usr/bin/env python3

import os
import sys
import json
import subprocess
import re


def parse_os_release():
    """Parse /etc/os-release file"""
    try:
        with open('/etc/os-release', 'r') as f:
            content = f.read()

        os_info = {}
        for line in content.split('\n'):
            if '=' in line and not line.startswith('#'):
                key, value = line.split('=', 1)
                # Remove quotes
                value = value.strip('"\'')
                os_info[key] = value

        return os_info
    except Exception:
        return {}


def get_kernel_info():
    """Get kernel version and architecture"""
    try:
        # Get kernel release
        result_r = subprocess.run(['uname', '-r'], capture_output=True, text=True, timeout=5)
        kernel_release = result_r.stdout.strip() if result_r.returncode == 0 else "unknown"

        # Get architecture
        result_m = subprocess.run(['uname', '-m'], capture_output=True, text=True, timeout=5)
        arch = result_m.stdout.strip() if result_m.returncode == 0 else "unknown"

        return kernel_release, arch
    except Exception:
        return "unknown", "unknown"


def parse_kernel_version(kernel_release):
    """Parse kernel version to get major.minor"""
    try:
        # Extract version like "5.15.0" from "5.15.0-91-generic"
        match = re.match(r'(\d+)\.(\d+)\.(\d+)', kernel_release)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            return major, minor
        return 0, 0
    except Exception:
        return 0, 0


def check_required_packages():
    """Check if required packages are available"""
    required_pkgs = ["bash", "curl", "jq", "lsblk", "ip"]
    missing_pkgs = []
    available_pkgs = []

    for pkg in required_pkgs:
        try:
            result = subprocess.run(['which', pkg], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                available_pkgs.append(pkg)
            else:
                missing_pkgs.append(pkg)
        except Exception:
            missing_pkgs.append(pkg)

    return available_pkgs, missing_pkgs


def get_uptime():
    """Get system uptime in days"""
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.read().split()[0])
        uptime_days = round(uptime_seconds / 86400, 1)
        return uptime_days
    except Exception:
        return 0.0


def check_time_sync():
    """Check time synchronization"""
    try:
        # Try chronyc first
        try:
            result = subprocess.run(['chronyc', 'tracking'],
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if 'System time' in line:
                        # Extract offset value
                        match = re.search(r'System time\s*:\s*([\d.+-]+)', line)
                        if match:
                            offset = float(match.group(1))
                            return f"chrony_offset_{offset:.3f}s"
                return "chrony_unknown"
        except Exception:
            pass

        # Fallback to timedatectl
        try:
            result = subprocess.run(['timedatectl', 'show'],
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if 'NTPSynchronized=' in line:
                        value = line.split('=')[1].strip()
                        return f"systemd_ntp_{value}"
                return "systemd_unknown"
        except Exception:
            pass

        return "unknown"
    except Exception:
        return "unknown"


def main():
    try:
        # Parse OS release
        os_info = parse_os_release()
        os_name = os_info.get('NAME', 'unknown')
        os_version = os_info.get('VERSION', 'unknown')
        os_id = os_info.get('ID', 'unknown')
        os_version_id = os_info.get('VERSION_ID', 'unknown')

        # Get kernel info
        kernel_release, arch = get_kernel_info()
        kernel_major, kernel_minor = parse_kernel_version(kernel_release)

        # Check if OS is supported
        supported_os = False
        if os_id == 'ubuntu' and os_version_id in ['20.04', '22.04', '24.04']:
            supported_os = True
        elif os_id in ['rhel', 'rocky', 'almalinux', 'centos'] and os_version_id in ['8', '9']:
            supported_os = True

        # Check kernel version (warn if < 5.4)
        kernel_version_ok = True
        if kernel_major < 5 or (kernel_major == 5 and kernel_minor < 4):
            kernel_version_ok = False

        # Check required packages
        available_pkgs, missing_pkgs = check_required_packages()

        # Get uptime
        uptime_days = get_uptime()

        # Check time sync
        time_sync = check_time_sync()

        # Determine status
        status = "pass"
        if not kernel_version_ok:
            status = "warn"

        # Build detail string
        detail_parts = [
            f"name={os_name.replace(' ', '_').replace('=', '-')}",
            f"version={os_version.replace(' ', '_').replace('=', '-')}",
            f"id={os_id}",
            f"version_id={os_version_id}",
            f"kernel={kernel_release}",
            f"arch={arch}",
            f"supported_os={str(supported_os).lower()}",
            f"kernel_ok={str(kernel_version_ok).lower()}",
            f"available_pkgs={','.join(available_pkgs)}",
            f"missing_pkgs={','.join(missing_pkgs) if missing_pkgs else 'none'}",
            f"uptime_days={uptime_days}",
            f"time_sync={time_sync}"
        ]

        detail = "|".join(detail_parts)

        result = {
            "check": "sw_os_version",
            "status": status,
            "detail": detail
        }

        print(json.dumps(result))

    except Exception as e:
        # Log error to stderr and output fail status
        print(f"Error in sw_os_version check: {e}", file=sys.stderr)
        result = {
            "check": "sw_os_version",
            "status": "fail",
            "detail": "error=exception"
        }
        print(json.dumps(result))


if __name__ == "__main__":
    main()
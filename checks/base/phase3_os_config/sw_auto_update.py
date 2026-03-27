#!/usr/bin/env python3

import os
import sys
import json
import subprocess


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


def check_systemctl_service(service_name):
    """Check systemctl service enabled/active status"""
    try:
        # Check if enabled
        result_enabled = subprocess.run(['systemctl', 'is-enabled', service_name],
                                      capture_output=True, text=True, timeout=5)
        enabled_status = result_enabled.stdout.strip()

        # Check if active
        result_active = subprocess.run(['systemctl', 'is-active', service_name],
                                     capture_output=True, text=True, timeout=5)
        active_status = result_active.stdout.strip()

        return enabled_status, active_status
    except Exception:
        return "unknown", "unknown"


def check_apt_auto_upgrades():
    """Check APT automatic upgrades configuration"""
    try:
        # Check /etc/apt/apt.conf.d/20auto-upgrades
        config_file = '/etc/apt/apt.conf.d/20auto-upgrades'
        unattended_upgrades = False
        auto_upgrades = False

        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                content = f.read()

            for line in content.split('\n'):
                if 'APT::Periodic::Unattended-Upgrade' in line and '"1"' in line:
                    unattended_upgrades = True
                elif 'APT::Periodic::Update-Package-Lists' in line and '"1"' in line:
                    auto_upgrades = True

        return unattended_upgrades, auto_upgrades
    except Exception:
        return False, False


def check_snap_refresh():
    """Check Snap refresh hold status"""
    try:
        result = subprocess.run(['snap', 'get', 'system', 'refresh.hold'],
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            hold_value = result.stdout.strip()
            return hold_value if hold_value else "none"
        return "unknown"
    except Exception:
        return "unknown"


def main():
    try:
        # Parse OS release
        os_info = parse_os_release()
        os_id = os_info.get('ID', 'unknown')
        os_id_like = os_info.get('ID_LIKE', '')

        # Initialize status variables
        unattended_enabled = False
        unattended_active = False
        auto_update_services = []

        # Check based on OS type
        if os_id in ['ubuntu', 'debian'] or 'ubuntu' in os_id_like or 'debian' in os_id_like:
            # Ubuntu/Debian systems
            # Check unattended-upgrades service
            unatt_enabled, unatt_active = check_systemctl_service("unattended-upgrades")
            unattended_enabled = unatt_enabled in ["enabled", "static"]
            unattended_active = unatt_active == "active"

            # Check apt-daily-upgrade timer
            daily_enabled, daily_active = check_systemctl_service("apt-daily-upgrade.timer")
            if daily_enabled in ["enabled", "static"]:
                auto_update_services.append("apt-daily-upgrade.timer")

            # Check APT configuration
            apt_unattended, apt_auto = check_apt_auto_upgrades()
            if apt_unattended:
                auto_update_services.append("apt-auto-upgrades")

        elif os_id in ['rhel', 'rocky', 'almalinux', 'centos', 'fedora'] or 'rhel' in os_id_like:
            # RHEL-based systems
            # Check dnf-automatic
            dnf_enabled, dnf_active = check_systemctl_service("dnf-automatic.timer")
            if dnf_enabled in ["enabled", "static"]:
                auto_update_services.append("dnf-automatic")
                unattended_enabled = True
                unattended_active = dnf_active == "active"

            # Check yum-cron (older systems)
            yum_enabled, yum_active = check_systemctl_service("yum-cron")
            if yum_enabled in ["enabled", "static"]:
                auto_update_services.append("yum-cron")
                unattended_enabled = True
                unattended_active = yum_active == "active"

        # Check Snap refresh
        snap_refresh_hold = check_snap_refresh()

        # Determine status
        status = "pass"
        if unattended_enabled or unattended_active:
            status = "fail"

        # Build detail string
        detail_parts = [
            f"os_id={os_id}",
            f"unattended_enabled={str(unattended_enabled).lower()}",
            f"unattended_active={str(unattended_active).lower()}",
            f"auto_services={','.join(auto_update_services) if auto_update_services else 'none'}",
            f"snap_refresh_hold={snap_refresh_hold}"
        ]

        detail = "|".join(detail_parts)

        result = {
            "check": "sw_auto_update",
            "status": status,
            "detail": detail
        }

        print(json.dumps(result))

    except Exception as e:
        # Log error to stderr and output fail status
        print(f"Error in sw_auto_update check: {e}", file=sys.stderr)
        result = {
            "check": "sw_auto_update",
            "status": "fail",
            "detail": "error=exception"
        }
        print(json.dumps(result))


if __name__ == "__main__":
    main()
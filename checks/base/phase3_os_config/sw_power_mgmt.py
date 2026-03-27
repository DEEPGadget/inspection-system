#!/usr/bin/env python3

import os
import sys
import json
import glob
import subprocess


def check_systemctl_service(service_name):
    """Check systemctl service enabled status"""
    try:
        result = subprocess.run(['systemctl', 'is-enabled', service_name],
                              capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except Exception:
        return "unknown"


def read_sysfs_file(path):
    """Read a sysfs file safely"""
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except Exception:
        return None


def check_cpu_governors():
    """Check CPU frequency governors"""
    try:
        governor_files = glob.glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor')
        if not governor_files:
            return "unknown", 0

        governors = []
        non_performance_count = 0

        for gov_file in governor_files:
            try:
                with open(gov_file, 'r') as f:
                    governor = f.read().strip()
                    governors.append(governor)
                    if governor != "performance":
                        non_performance_count += 1
            except Exception:
                continue

        # Get the governor for cpu0 as primary
        primary_governor = "unknown"
        if governors:
            primary_governor = governors[0]

        return primary_governor, non_performance_count
    except Exception:
        return "unknown", 0


def check_tuned_profile():
    """Check tuned active profile"""
    try:
        result = subprocess.run(['tuned-adm', 'active'],
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'Current active profile:' in line:
                    profile = line.split(':')[1].strip()
                    return profile
        return "unknown"
    except Exception:
        return "unknown"


def main():
    try:
        # Check sleep targets
        sleep_enabled = check_systemctl_service("sleep.target")
        suspend_enabled = check_systemctl_service("suspend.target")
        hibernate_enabled = check_systemctl_service("hibernate.target")
        hybrid_sleep_enabled = check_systemctl_service("hybrid-sleep.target")

        # Check CPU governors
        primary_governor, non_perf_cores = check_cpu_governors()

        # Check C-states
        max_cstate = read_sysfs_file("/sys/module/intel_idle/parameters/max_cstate")
        if max_cstate is None:
            max_cstate = "unknown"

        # Check turbo boost
        no_turbo = read_sysfs_file("/sys/devices/system/cpu/intel_pstate/no_turbo")
        if no_turbo == "0":
            turbo_boost = "enabled"
        elif no_turbo == "1":
            turbo_boost = "disabled"
        else:
            turbo_boost = "unknown"

        # Check tuned profile
        tuned_profile = check_tuned_profile()

        # Determine status
        status = "pass"

        # FAIL conditions
        if sleep_enabled != "masked":
            status = "fail"
        elif primary_governor != "performance":
            status = "fail"
        # WARN conditions
        elif suspend_enabled not in ["masked", "disabled"]:
            status = "warn"
        elif hibernate_enabled not in ["masked", "disabled"]:
            status = "warn"
        elif hybrid_sleep_enabled not in ["masked", "disabled"]:
            status = "warn"
        elif max_cstate != "unknown" and max_cstate.isdigit() and int(max_cstate) > 1:
            status = "warn"
        elif tuned_profile not in ["throughput-performance", "latency-performance", "network-latency"]:
            status = "warn"

        # Build detail string
        detail_parts = [
            f"sleep_target={sleep_enabled}",
            f"suspend_target={suspend_enabled}",
            f"hibernate_target={hibernate_enabled}",
            f"hybrid_sleep_target={hybrid_sleep_enabled}",
            f"cpu_governor={primary_governor}",
            f"non_perf_cores={non_perf_cores}",
            f"max_cstate={max_cstate}",
            f"turbo_boost={turbo_boost}",
            f"tuned_profile={tuned_profile}"
        ]

        detail = "|".join(detail_parts)

        result = {
            "check": "sw_power_mgmt",
            "status": status,
            "detail": detail
        }

        print(json.dumps(result))

    except Exception as e:
        # Log error to stderr and output fail status
        print(f"Error in sw_power_mgmt check: {e}", file=sys.stderr)
        result = {
            "check": "sw_power_mgmt",
            "status": "fail",
            "detail": "error=exception"
        }
        print(json.dumps(result))


if __name__ == "__main__":
    main()
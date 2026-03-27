#!/usr/bin/env python3

import os
import sys
import json
import glob
import re
import subprocess
from pathlib import Path


def parse_cpuinfo():
    """Parse /proc/cpuinfo for CPU information"""
    try:
        with open('/proc/cpuinfo', 'r') as f:
            content = f.read()

        # Parse CPU model name
        model_match = re.search(r'model name\s*:\s*(.+)', content)
        cpu_model = model_match.group(1).strip() if model_match else "unknown"

        # Count physical CPUs and cores
        physical_ids = set()
        cpu_cores_per_socket = 0
        processor_count = 0

        for line in content.split('\n'):
            if line.startswith('physical id'):
                phys_id = line.split(':')[1].strip()
                physical_ids.add(phys_id)
            elif line.startswith('cpu cores'):
                cpu_cores_per_socket = int(line.split(':')[1].strip())
            elif line.startswith('processor'):
                processor_count += 1

        cpu_sockets = len(physical_ids) if physical_ids else 1
        cpu_cores_total = cpu_sockets * cpu_cores_per_socket if cpu_cores_per_socket else processor_count

        return cpu_model, cpu_sockets, cpu_cores_total, processor_count
    except Exception:
        return "unknown", 0, 0, 0


def get_cpu_freq_ghz():
    """Get CPU max frequency in GHz"""
    try:
        with open('/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq', 'r') as f:
            freq_khz = int(f.read().strip())
        return round(freq_khz / 1000000, 2)
    except Exception:
        return 0.0


def get_cpu_temp():
    """Get CPU temperature in Celsius"""
    try:
        # Check thermal zones first
        thermal_zones = glob.glob('/sys/class/thermal/thermal_zone*/type')
        max_temp = 0

        for zone_type_path in thermal_zones:
            try:
                with open(zone_type_path, 'r') as f:
                    zone_type = f.read().strip()

                # Look for CPU-related thermal zones
                if zone_type in ['x86_pkg_temp', 'acpitz', 'cpu']:
                    zone_dir = os.path.dirname(zone_type_path)
                    temp_path = os.path.join(zone_dir, 'temp')

                    with open(temp_path, 'r') as f:
                        temp_millicelsius = int(f.read().strip())
                        temp_celsius = temp_millicelsius / 1000
                        max_temp = max(max_temp, temp_celsius)
            except Exception:
                continue

        if max_temp > 0:
            return max_temp

        # Fallback to sensors command
        try:
            result = subprocess.run(['sensors'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    # Look for "Package id N: +XX.X°C" pattern
                    match = re.search(r'Package id \d+:\s*\+?(\d+\.\d+)°?C', line)
                    if match:
                        temp = float(match.group(1))
                        max_temp = max(max_temp, temp)

                if max_temp > 0:
                    return max_temp
        except Exception:
            pass

        return None
    except Exception:
        return None


def main():
    try:
        # Parse CPU info
        cpu_model, cpu_sockets, cpu_cores_total, processor_count = parse_cpuinfo()

        # Get CPU frequency
        cpu_freq_ghz = get_cpu_freq_ghz()

        # Get CPU temperature
        cpu_max_temp_c = get_cpu_temp()

        # Determine status
        status = "pass"
        if cpu_max_temp_c is not None and cpu_max_temp_c > 100:
            status = "fail"
        elif cpu_max_temp_c is None:
            status = "warn"

        # Build detail string
        detail_parts = [
            f"model={cpu_model.replace(' ', '_').replace('=', '-')}",
            f"sockets={cpu_sockets}",
            f"cores={cpu_cores_total}",
            f"threads={processor_count}",
            f"freq_ghz={cpu_freq_ghz}"
        ]

        if cpu_max_temp_c is not None:
            detail_parts.append(f"max_temp_c={cpu_max_temp_c:.1f}")
        else:
            detail_parts.append("max_temp_c=unknown")

        detail = "|".join(detail_parts)

        result = {
            "check": "sw_cpu",
            "status": status,
            "detail": detail
        }

        print(json.dumps(result))

    except Exception as e:
        # Log error to stderr and output fail status
        print(f"Error in sw_cpu check: {e}", file=sys.stderr)
        result = {
            "check": "sw_cpu",
            "status": "fail",
            "detail": "error=exception"
        }
        print(json.dumps(result))


if __name__ == "__main__":
    main()
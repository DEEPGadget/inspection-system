#!/usr/bin/env python3

import os
import sys
import json
import glob
import re
import subprocess


def parse_cpuinfo():
    """Parse /proc/cpuinfo for CPU information"""
    try:
        with open("/proc/cpuinfo", "r") as f:
            content = f.read()

        # Parse CPU model name
        model_match = re.search(r"model name\s*:\s*(.+)", content)
        cpu_model = model_match.group(1).strip() if model_match else "unknown"

        # Count physical CPUs and cores
        physical_ids = set()
        cpu_cores_per_socket = 0
        processor_count = 0

        for line in content.split("\n"):
            if line.startswith("physical id"):
                phys_id = line.split(":")[1].strip()
                physical_ids.add(phys_id)
            elif line.startswith("cpu cores"):
                cpu_cores_per_socket = int(line.split(":")[1].strip())
            elif line.startswith("processor"):
                processor_count += 1

        cpu_sockets = len(physical_ids) if physical_ids else 1
        cpu_cores_total = (
            cpu_sockets * cpu_cores_per_socket if cpu_cores_per_socket else processor_count
        )

        return cpu_model, cpu_sockets, cpu_cores_total, processor_count
    except Exception:
        return "unknown", 0, 0, 0


def get_cpu_freq_ghz():
    """Get CPU max frequency in GHz"""
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "r") as f:
            freq_khz = int(f.read().strip())
        return round(freq_khz / 1000000, 2)
    except Exception:
        return 0.0


def get_cpu_temp():
    """Get CPU package temperature in Celsius.

    Intel: coretemp 칩의 "Package id N" 레이블.
    AMD:   k10temp 칩의 Tctl (제어 온도) 또는 Tdie (다이 온도).
           Tccd* (CCD별 온도)는 노이즈라 제외.
    탐색 순서: hwmon sysfs → thermal_zone → sensors 명령.
    """
    max_temp = 0.0

    # 1. hwmon sysfs (most reliable, no subprocess)
    for name_path in glob.glob("/sys/class/hwmon/hwmon*/name"):
        try:
            with open(name_path, "r") as f:
                chip = f.read().strip()
            if chip not in ("coretemp", "k10temp", "zenpower"):
                continue
            hwmon_dir = os.path.dirname(name_path)
            for temp_input in sorted(glob.glob(f"{hwmon_dir}/temp*_input")):
                label_path = temp_input.replace("_input", "_label")
                label = ""
                if os.path.exists(label_path):
                    with open(label_path, "r") as f:
                        label = f.read().strip()
                # AMD k10temp: Tctl / Tdie만 채택 (Tccd*는 per-CCD 노이즈)
                if chip in ("k10temp", "zenpower"):
                    if label and label not in ("Tctl", "Tdie"):
                        continue
                # Intel coretemp: "Package id N"만 채택 (Core N은 per-core)
                elif chip == "coretemp":
                    if label and not label.startswith("Package"):
                        continue
                with open(temp_input, "r") as f:
                    temp_c = int(f.read().strip()) / 1000
                    max_temp = max(max_temp, temp_c)
        except Exception:
            continue

    if max_temp > 0:
        return max_temp

    # 2. Legacy thermal_zone (older Intel mobile/desktop)
    for zone_type_path in glob.glob("/sys/class/thermal/thermal_zone*/type"):
        try:
            with open(zone_type_path, "r") as f:
                zone_type = f.read().strip()
            if zone_type not in ("x86_pkg_temp", "acpitz", "cpu"):
                continue
            temp_path = os.path.join(os.path.dirname(zone_type_path), "temp")
            with open(temp_path, "r") as f:
                temp_c = int(f.read().strip()) / 1000
                max_temp = max(max_temp, temp_c)
        except Exception:
            continue

    if max_temp > 0:
        return max_temp

    # 3. sensors command fallback (AMD Tctl/Tdie + Intel Package)
    try:
        result = subprocess.run(["sensors"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                match = re.search(r"(?:Package id \d+|Tctl|Tdie):\s*\+?(\d+\.\d+)°?C", line)
                if match:
                    temp = float(match.group(1))
                    max_temp = max(max_temp, temp)
            if max_temp > 0:
                return max_temp
    except Exception:
        pass

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
            f"freq_ghz={cpu_freq_ghz}",
        ]

        if cpu_max_temp_c is not None:
            detail_parts.append(f"max_temp_c={cpu_max_temp_c:.1f}")
        else:
            detail_parts.append("max_temp_c=unknown")

        detail = "|".join(detail_parts)

        result = {"check": "sw_cpu", "status": status, "detail": detail}

        print(json.dumps(result))

    except Exception as e:
        # Log error to stderr and output fail status
        print(f"Error in sw_cpu check: {e}", file=sys.stderr)
        result = {"check": "sw_cpu", "status": "fail", "detail": "error=exception"}
        print(json.dumps(result))


if __name__ == "__main__":
    main()

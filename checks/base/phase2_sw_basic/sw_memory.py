#!/usr/bin/env python3

import os
import sys
import json
import glob
import subprocess
import re


def parse_meminfo():
    """Parse /proc/meminfo for memory information"""
    try:
        with open('/proc/meminfo', 'r') as f:
            content = f.read()

        mem_total_kb = 0
        mem_available_kb = 0
        swap_total_kb = 0

        for line in content.split('\n'):
            if line.startswith('MemTotal:'):
                mem_total_kb = int(line.split()[1])
            elif line.startswith('MemAvailable:'):
                mem_available_kb = int(line.split()[1])
            elif line.startswith('SwapTotal:'):
                swap_total_kb = int(line.split()[1])

        total_gb = round(mem_total_kb / 1024 / 1024, 1)
        available_gb = round(mem_available_kb / 1024 / 1024, 1)
        swap_gb = round(swap_total_kb / 1024 / 1024, 1)

        return total_gb, available_gb, swap_gb
    except Exception:
        return 0.0, 0.0, 0.0


def get_dimm_count():
    """Get DIMM count using dmidecode"""
    try:
        result = subprocess.run(['dmidecode', '-t', 'memory'],
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            dimm_count = 0
            for line in result.stdout.split('\n'):
                # Look for "Size: X GB" pattern
                if re.search(r'Size:\s*\d+.*GB', line):
                    dimm_count += 1
            return dimm_count
        return 0
    except Exception:
        return 0


def get_numa_nodes():
    """Get NUMA node count"""
    try:
        numa_nodes = glob.glob('/sys/devices/system/node/node*')
        # Filter out non-numeric node names
        numa_count = 0
        for node_path in numa_nodes:
            node_name = os.path.basename(node_path)
            if node_name.startswith('node') and node_name[4:].isdigit():
                numa_count += 1
        return numa_count
    except Exception:
        return 0


def check_memory_errors():
    """Check for memory errors using edac-util or sysfs"""
    try:
        # Try edac-util first
        try:
            result = subprocess.run(['edac-util', '-s', '0'],
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                error_count = 0
                for line in result.stdout.split('\n'):
                    if 'error' in line.lower():
                        error_count += 1
                return error_count, "edac-util"
        except Exception:
            pass

        # Fallback: check if APEI driver exists
        if os.path.exists('/sys/bus/platform/drivers/APEI'):
            return 0, "apei"

        return 0, "none"
    except Exception:
        return 0, "unknown"


def main():
    try:
        # Parse memory info
        total_gb, available_gb, swap_gb = parse_meminfo()

        # Get DIMM count
        dimm_count = get_dimm_count()

        # Get NUMA nodes
        numa_nodes = get_numa_nodes()

        # Check memory errors
        memory_errors, error_method = check_memory_errors()

        # Determine status
        status = "pass"
        if total_gb < 64:
            status = "warn"

        # Build detail string
        detail_parts = [
            f"total_gb={total_gb}",
            f"available_gb={available_gb}",
            f"swap_gb={swap_gb}",
            f"dimm_count={dimm_count}",
            f"numa_nodes={numa_nodes}",
            f"memory_errors={memory_errors}",
            f"error_method={error_method}"
        ]

        detail = "|".join(detail_parts)

        result = {
            "check": "sw_memory",
            "status": status,
            "detail": detail
        }

        print(json.dumps(result))

    except Exception as e:
        # Log error to stderr and output fail status
        print(f"Error in sw_memory check: {e}", file=sys.stderr)
        result = {
            "check": "sw_memory",
            "status": "fail",
            "detail": "error=exception"
        }
        print(json.dumps(result))


if __name__ == "__main__":
    main()
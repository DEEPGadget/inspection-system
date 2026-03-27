#!/usr/bin/env python3

import os
import sys
import json
import subprocess
import re
from collections import Counter


def run_nvidia_smi(query, timeout=10):
    """Run nvidia-smi command with timeout"""
    try:
        cmd = f"nvidia-smi --query-gpu={query} --format=csv,noheader,nounits"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip().split('\n')
        return None
    except Exception:
        return None


def check_nvidia_smi():
    """Check if nvidia-smi is available"""
    try:
        result = subprocess.run(['which', 'nvidia-smi'], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def get_cuda_version():
    """Get CUDA version from nvidia-smi"""
    try:
        result = subprocess.run(['nvidia-smi'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'CUDA Version' in line:
                    match = re.search(r'CUDA Version:\s*(\d+\.\d+)', line)
                    if match:
                        return match.group(1)
        return "unknown"
    except Exception:
        return "unknown"


def get_nvlink_status():
    """Get NVLink status"""
    try:
        result = subprocess.run(['nvidia-smi', 'nvlink', '--status'],
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            active_count = 0
            for line in result.stdout.split('\n'):
                if 'Active' in line:
                    active_count += 1
            return active_count
        return 0
    except Exception:
        return 0


def main():
    try:
        # Check if nvidia-smi exists
        if not check_nvidia_smi():
            result = {
                "check": "sw_gpu",
                "status": "fail",
                "detail": "nvidia_smi=missing"
            }
            print(json.dumps(result))
            return

        # Get GPU names and count
        gpu_names = run_nvidia_smi("name")
        if not gpu_names or gpu_names == ['']:
            result = {
                "check": "sw_gpu",
                "status": "fail",
                "detail": "gpu_count=0"
            }
            print(json.dumps(result))
            return

        gpu_count = len(gpu_names)
        gpu_models = Counter(gpu_names)
        gpu_model_str = "|".join([f"{model.replace(' ', '_').replace('=', '-')}x{count}"
                                 for model, count in gpu_models.items()])

        # Get driver version
        driver_versions = run_nvidia_smi("driver_version")
        driver_version = driver_versions[0] if driver_versions else "unknown"

        # Get CUDA version
        cuda_version = get_cuda_version()

        # Get VRAM total (GB)
        vram_mb_list = run_nvidia_smi("memory.total")
        vram_total_gb = 0
        if vram_mb_list:
            try:
                vram_total_gb = sum(int(float(vram)) for vram in vram_mb_list) // 1024
            except ValueError:
                vram_total_gb = 0

        # Get GPU temperatures
        temp_list = run_nvidia_smi("temperature.gpu")
        gpu_max_temp_c = 0
        if temp_list:
            try:
                temps = [int(float(temp)) for temp in temp_list if temp.strip()]
                gpu_max_temp_c = max(temps) if temps else 0
            except ValueError:
                gpu_max_temp_c = 0

        # Get power draw total
        power_list = run_nvidia_smi("power.draw")
        power_total_w = 0
        if power_list:
            try:
                # Handle "N/A" values
                powers = []
                for power in power_list:
                    if power.strip() and power.strip().upper() != "N/A":
                        powers.append(float(power))
                power_total_w = int(sum(powers)) if powers else 0
            except ValueError:
                power_total_w = 0

        # Get ECC uncorrected errors
        ecc_list = run_nvidia_smi("ecc.errors.uncorrected.volatile.total")
        ecc_uncorrected = 0
        if ecc_list:
            try:
                ecc_errors = []
                for ecc in ecc_list:
                    if ecc.strip() and ecc.strip().upper() != "N/A":
                        ecc_errors.append(int(ecc))
                ecc_uncorrected = sum(ecc_errors) if ecc_errors else 0
            except ValueError:
                ecc_uncorrected = 0

        # Get NVLink active links
        nvlink_active = get_nvlink_status()

        # Get persistence mode
        persistence_list = run_nvidia_smi("persistence_mode")
        persistence_enabled = all(p.strip() == "Enabled" for p in persistence_list if p.strip())

        # Determine status
        status = "pass"
        if gpu_max_temp_c > 87:
            status = "fail"
        elif ecc_uncorrected > 0:
            status = "fail"
        elif not persistence_enabled:
            status = "warn"

        # Build detail string
        detail_parts = [
            f"count={gpu_count}",
            f"models={gpu_model_str}",
            f"driver={driver_version}",
            f"cuda={cuda_version}",
            f"vram_gb={vram_total_gb}",
            f"max_temp_c={gpu_max_temp_c}",
            f"power_w={power_total_w}",
            f"ecc_uncorrected={ecc_uncorrected}",
            f"nvlink_active={nvlink_active}",
            f"persistence={'enabled' if persistence_enabled else 'disabled'}"
        ]

        detail = "|".join(detail_parts)

        result = {
            "check": "sw_gpu",
            "status": status,
            "detail": detail
        }

        print(json.dumps(result))

    except Exception as e:
        # Log error to stderr and output fail status
        print(f"Error in sw_gpu check: {e}", file=sys.stderr)
        result = {
            "check": "sw_gpu",
            "status": "fail",
            "detail": "error=exception"
        }
        print(json.dumps(result))


if __name__ == "__main__":
    main()
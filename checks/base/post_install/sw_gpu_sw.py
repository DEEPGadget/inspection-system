#!/usr/bin/env python3
"""
GPU S/W 검수 — nvidia-smi로 driver·VRAM·온도·ECC·NVLink 확인.
preflight 단계의 sw_gpu_hw.py와 역할 분리:
  sw_gpu_hw.py: H/W 관점 (driver 불필요, lspci)
  이 스크립트: S/W 관점 (driver 필요, nvidia-smi)
"""

import json
import re
import subprocess
import sys
from collections import Counter


def _smi(query: str, timeout: int = 10) -> list[str] | None:
    cmd = f"nvidia-smi --query-gpu={query} --format=csv,noheader,nounits"
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip().split("\n")
        return None
    except Exception:
        return None


def _cuda_version() -> str:
    try:
        result = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "CUDA Version" in line:
                    m = re.search(r"CUDA Version:\s*([\d.]+)", line)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return "unknown"


def _nvlink_active() -> int:
    try:
        result = subprocess.run(
            ["nvidia-smi", "nvlink", "--status"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return sum(1 for line in result.stdout.split("\n") if "Active" in line)
    except Exception:
        pass
    return 0


def main():
    try:
        # nvidia-smi 존재 확인
        if subprocess.run(["which", "nvidia-smi"], capture_output=True, timeout=5).returncode != 0:
            print(
                json.dumps({"check": "sw_gpu_sw", "status": "fail", "detail": "nvidia_smi=missing"})
            )
            return

        gpu_names = _smi("name")
        if not gpu_names or gpu_names == [""]:
            print(json.dumps({"check": "sw_gpu_sw", "status": "fail", "detail": "gpu_count=0"}))
            return

        gpu_count = len(gpu_names)
        gpu_model_str = "|".join(
            f"{m.replace(' ', '_')}x{c}" for m, c in Counter(gpu_names).items()
        )

        driver_versions = _smi("driver_version")
        driver_version = driver_versions[0] if driver_versions else "unknown"

        cuda_version = _cuda_version()

        vram_mb_list = _smi("memory.total")
        vram_total_gb = 0
        if vram_mb_list:
            try:
                vram_total_gb = sum(int(float(v)) for v in vram_mb_list) // 1024
            except ValueError:
                pass

        temp_list = _smi("temperature.gpu")
        gpu_max_temp_c = 0
        if temp_list:
            try:
                temps = [int(float(t)) for t in temp_list if t.strip()]
                gpu_max_temp_c = max(temps) if temps else 0
            except ValueError:
                pass

        power_list = _smi("power.draw")
        power_total_w = 0
        if power_list:
            try:
                powers = [float(p) for p in power_list if p.strip() and p.strip().upper() != "N/A"]
                power_total_w = int(sum(powers)) if powers else 0
            except ValueError:
                pass

        ecc_list = _smi("ecc.errors.uncorrected.volatile.total")
        ecc_delta_uncorr = 0
        if ecc_list:
            try:
                errs = [int(e) for e in ecc_list if e.strip() and e.strip().upper() != "N/A"]
                ecc_delta_uncorr = sum(errs)
            except ValueError:
                pass

        nvlink_active = _nvlink_active()

        persistence_list = _smi("persistence_mode")
        persistence_enabled = (
            all(p.strip() == "Enabled" for p in persistence_list if p.strip())
            if persistence_list
            else False
        )

        # 판정
        status = "pass"
        if gpu_max_temp_c > 87:
            status = "fail"
        elif ecc_delta_uncorr > 0:
            status = "fail"
        elif not persistence_enabled:
            status = "warn"

        detail = "|".join(
            [
                f"gpu_count={gpu_count}",
                f"models={gpu_model_str}",
                f"driver={driver_version}",
                f"cuda={cuda_version}",
                f"vram_gb={vram_total_gb}",
                f"gpu_max_temp_c={gpu_max_temp_c}",
                f"power_w={power_total_w}",
                f"ecc_delta_uncorr={ecc_delta_uncorr}",
                f"nvlink_active={nvlink_active}",
                f"persistence={'enabled' if persistence_enabled else 'disabled'}",
            ]
        )

        print(json.dumps({"check": "sw_gpu_sw", "status": status, "detail": detail}))

    except Exception as e:
        print(f"Error in sw_gpu_sw: {e}", file=sys.stderr)
        print(json.dumps({"check": "sw_gpu_sw", "status": "fail", "detail": "error=exception"}))


if __name__ == "__main__":
    main()

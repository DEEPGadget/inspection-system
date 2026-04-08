#!/usr/bin/env python3
"""stress_gpu — GPU 스트레스 테스트 (gpu_burn 전용)
Phase4: 부하 중 온도/전력/Utilization/Slowdown/ECC 모니터링

기대 동작:
  1. ~/gpu-burn/gpu_burn 바이너리 확인
  2. 없으면 git clone → make 빌드 (~ 하위, sudo 불필요)
  3. gpu_burn -d -tc <duration> 실행 (FP64 + Tensor Core)
  4. 빌드/실행 실패 시 사유(stderr 마지막 5줄)와 함께 fail 반환

환경변수:
  GPU_BURNIN_DURATION  부하 지속 시간(초) [기본: 300]

FAIL: peak_temp > 87°C | HW throttle | ECC uncorrected 증가
      풀로드(util≥80%) + 저전력(power_ratio<70%)
      gpu_burn 확보/실행 실패
WARN: SW/PWR throttle | ECC corrected 증가 | util<80%
출력: {"check":"stress_gpu","status":"pass|fail|warn","detail":"..."}
"""

import json
import os
import subprocess
import sys
import time

CHECK = "stress_gpu"
GPU_BURN_DIR = os.path.expanduser("~/gpu-burn")
GPU_BURN_BIN = f"{GPU_BURN_DIR}/gpu_burn"
GPU_BURN_REPO = "https://github.com/wilicc/gpu-burn.git"


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def run_full(cmd, timeout=10):
    """returncode/stdout/stderr 전체 반환."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        return -1, "", f"timeout after {timeout}s: {e}"
    except Exception as e:
        return -1, "", str(e)


def tail_lines(text: str, n: int = 5) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return " // ".join(lines[-n:])


def emit(status, details):
    print(
        json.dumps(
            {"check": CHECK, "status": status, "detail": "|".join(details)}, ensure_ascii=False
        )
    )
    sys.exit(0)


def ensure_gpu_burn(details: list[str]) -> str | None:
    """gpu_burn 바이너리 경로 반환. 확보 실패 시 details에 사유 추가하고 None 반환."""
    # 1) 이미 있으면 그대로 사용
    if os.path.isfile(GPU_BURN_BIN) and os.access(GPU_BURN_BIN, os.X_OK):
        return GPU_BURN_BIN

    # 2) 빌드 도구 점검
    missing = [t for t in ("nvcc", "git", "make") if not run(f"command -v {t}")]
    if missing:
        details.append(f"FAIL:gpu_burn_build_tools_missing={','.join(missing)}")
        return None

    # 3) 기존 디렉토리(부분 빌드 등) 정리
    subprocess.run(f"rm -rf {GPU_BURN_DIR}", shell=True)

    # 4) git clone
    rc, _, err = run_full(f"git clone --depth=1 {GPU_BURN_REPO} {GPU_BURN_DIR}", timeout=120)
    if rc != 0:
        details.append(f"FAIL:gpu_burn_clone_failed:{tail_lines(err)}")
        return None

    # 5) make
    rc, _, err = run_full(f"make -C {GPU_BURN_DIR}", timeout=300)
    if rc != 0:
        details.append(f"FAIL:gpu_burn_make_failed:{tail_lines(err)}")
        return None

    if os.path.isfile(GPU_BURN_BIN) and os.access(GPU_BURN_BIN, os.X_OK):
        return GPU_BURN_BIN

    details.append("FAIL:gpu_burn_binary_missing_after_build")
    return None


def main():
    status = "pass"
    details = []
    duration = int(os.environ.get("GPU_BURNIN_DURATION", "300"))

    # nvidia-smi 확인
    out = run("nvidia-smi", timeout=10)
    if not out:
        emit("fail", ["FAIL:nvidia-smi not found"])

    # GPU 수량
    gpu_names = run("nvidia-smi --query-gpu=name --format=csv,noheader", timeout=10)
    gpu_count = len([line for line in gpu_names.splitlines() if line.strip()]) if gpu_names else 0
    if gpu_count == 0:
        emit("fail", ["FAIL:no GPUs detected"])
    details.append(f"gpu_count={gpu_count}")

    # TDP
    tdp_raw = run("nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits", timeout=10)
    try:
        tdp = int(float(tdp_raw.splitlines()[0].strip())) if tdp_raw else 0
    except Exception:
        tdp = 0
    details.append(f"tdp_w={tdp}")

    # ECC 기준값 스냅샷
    def read_ecc(metric):
        out = run(f"nvidia-smi --query-gpu={metric} --format=csv,noheader,nounits", timeout=10)
        total = 0
        for line in out.splitlines():
            line = line.strip()
            if line and line != "N/A":
                try:
                    total += int(line)
                except Exception:
                    pass
        return total

    ecc_corr_before = read_ecc("ecc.errors.corrected.volatile.total")
    ecc_uncorr_before = read_ecc("ecc.errors.uncorrected.volatile.total")

    # gpu_burn 확보 (없으면 빌드)
    burn_bin = ensure_gpu_burn(details)
    if burn_bin is None:
        details.append(f"tool=none|duration_s={duration}")
        emit("fail", details)

    tool = "gpu_burn"
    details.append(f"tool={tool}")
    details.append(f"duration_s={duration}")

    # gpu_burn 실행: -d (FP64) + -tc (Tensor Core) + duration
    try:
        stress_proc = subprocess.Popen(
            [burn_bin, "-d", "-tc", str(duration)],
            cwd=GPU_BURN_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except Exception as e:
        details.append(f"FAIL:gpu_burn_launch_failed:{e}")
        emit("fail", details)

    # 모니터링 루프 (10초 간격)
    peak_temp = 0
    peak_power = 0
    util_sum = 0
    sample_count = 0
    slowdown_hw = 0
    slowdown_sw = 0
    slowdown_pwr = 0
    stress_died = False

    end_time = time.time() + duration

    while time.time() < end_time:
        if stress_proc.poll() is not None:
            stress_died = True
            break

        smi_out = run(
            "nvidia-smi --query-gpu=index,temperature.gpu,power.draw,"
            "utilization.gpu,clocks_throttle_reasons.active "
            "--format=csv,noheader,nounits",
            timeout=10,
        )
        for line in smi_out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            _, temp_s, power_s, util_s, throttle_s = (
                parts[0],
                parts[1],
                parts[2],
                parts[3],
                parts[4],
            )

            try:
                temp = int(temp_s)
                if temp > peak_temp:
                    peak_temp = temp
            except Exception:
                pass
            try:
                pwr = int(float(power_s))
                if pwr > peak_power:
                    peak_power = pwr
            except Exception:
                pass
            try:
                util = int(util_s)
                util_sum += util
                sample_count += 1
            except Exception:
                pass

            if throttle_s.startswith("0x"):
                try:
                    dec = int(throttle_s, 16)
                    if dec & 0x8:
                        slowdown_hw += 1
                    if dec & 0x4:
                        slowdown_sw += 1
                    if dec & 0x1:
                        slowdown_pwr += 1
                except Exception:
                    pass

        time.sleep(10)

    # stress 정리 + stderr 캡처
    burn_stderr = ""
    if stress_proc.poll() is None:
        stress_proc.terminate()
        try:
            _, burn_stderr = stress_proc.communicate(timeout=5)
        except Exception:
            stress_proc.kill()
            try:
                _, burn_stderr = stress_proc.communicate(timeout=5)
            except Exception:
                burn_stderr = ""
    else:
        try:
            _, burn_stderr = stress_proc.communicate(timeout=5)
        except Exception:
            burn_stderr = ""

    # ECC 사후 측정
    ecc_corr_after = read_ecc("ecc.errors.corrected.volatile.total")
    ecc_uncorr_after = read_ecc("ecc.errors.uncorrected.volatile.total")
    ecc_delta_corr = max(0, ecc_corr_after - ecc_corr_before)
    ecc_delta_uncorr = max(0, ecc_uncorr_after - ecc_uncorr_before)

    avg_util = util_sum // sample_count if sample_count > 0 else 0
    pwr_ratio = int(peak_power / tdp * 100) if tdp > 0 else 0

    details += [
        f"peak_temp_c={peak_temp}",
        f"peak_power_w={peak_power}",
        f"power_ratio_pct={pwr_ratio}",
        f"avg_util_pct={avg_util}",
        f"slowdown_hw={slowdown_hw}",
        f"slowdown_sw={slowdown_sw}",
        f"slowdown_pwr={slowdown_pwr}",
        f"ecc_corr_before={ecc_corr_before}",
        f"ecc_corr_after={ecc_corr_after}",
        f"ecc_delta_corr={ecc_delta_corr}",
        f"ecc_uncorr_before={ecc_uncorr_before}",
        f"ecc_uncorr_after={ecc_uncorr_after}",
        f"ecc_delta_uncorr={ecc_delta_uncorr}",
    ]

    # FAIL 판정
    if peak_temp > 87:
        status = "fail"
        details.append(f"FAIL:peak_temp_over_87c({peak_temp}c)")
    if slowdown_hw > 0:
        status = "fail"
        details.append(f"FAIL:hw_thermal_throttle_count={slowdown_hw}")
    if ecc_delta_uncorr > 0:
        status = "fail"
        details.append(f"FAIL:ecc_uncorrected_increased_by={ecc_delta_uncorr}")
    if avg_util >= 80 and pwr_ratio < 70 and tdp > 0:
        status = "fail"
        details.append(f"FAIL:full_load_low_power(util={avg_util}pct,ratio={pwr_ratio}pct_of_tdp)")
    if stress_died:
        status = "fail"
        details.append(f"FAIL:gpu_burn_exited_early:{tail_lines(burn_stderr)}")

    # WARN 판정
    if slowdown_sw > 0:
        if status == "pass":
            status = "warn"
        details.append(f"WARN:sw_thermal_slowdown_count={slowdown_sw}")
    if slowdown_pwr > 0:
        if status == "pass":
            status = "warn"
        details.append(f"WARN:power_cap_throttle_count={slowdown_pwr}")
    if ecc_delta_corr > 0:
        if status == "pass":
            status = "warn"
        details.append(f"WARN:ecc_corrected_increased_by={ecc_delta_corr}")
    if avg_util < 80 and sample_count > 0 and not stress_died:
        if status == "pass":
            status = "warn"
        details.append(f"WARN:low_gpu_utilization_avg={avg_util}pct")

    emit(status, details)


if __name__ == "__main__":
    main()

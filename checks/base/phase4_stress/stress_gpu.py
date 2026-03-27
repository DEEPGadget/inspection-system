#!/usr/bin/env python3
"""stress_gpu — GPU 스트레스 테스트
Phase4: 부하 중 온도/전력/Utilization/Slowdown/ECC 모니터링

환경변수:
  GPU_BURNIN_DURATION  부하 지속 시간(초) [기본: 300]

FAIL: peak_temp > 87°C | HW throttle 발생 | ECC uncorrected 증가
      풀로드(util≥80%) + 저전력(power_ratio<70%)
WARN: SW/PWR throttle | ECC corrected 증가 | util<80% | 도구 없음
출력: {"check":"stress_gpu","status":"pass|fail|warn","detail":"..."}
"""
import json
import os
import subprocess
import sys
import time

CHECK = "stress_gpu"
GPU_BURN_DIR = "/opt/gpu-burn"
NCCL_TESTS_DIR = "/opt/nccl-tests"


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def run_rc(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", -1


def emit(status, details):
    print(json.dumps({"check": CHECK, "status": status, "detail": "|".join(details)}, ensure_ascii=False))
    sys.exit(0)


def main():
    status = "pass"
    details = []
    duration = int(os.environ.get("GPU_BURNIN_DURATION", "300"))

    # nvidia-smi 확인
    out, rc = run_rc("nvidia-smi", timeout=10)
    if rc != 0 or not out:
        emit("fail", ["nvidia-smi not found"])

    # GPU 수량
    gpu_names = run("nvidia-smi --query-gpu=name --format=csv,noheader", timeout=10)
    gpu_count = len([l for l in gpu_names.splitlines() if l.strip()]) if gpu_names else 0
    if gpu_count == 0:
        emit("fail", ["no GPUs detected"])
    details.append(f"gpu_count={gpu_count}")

    # TDP
    tdp_raw = run("nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits", timeout=10)
    try:
        tdp = int(float(tdp_raw.splitlines()[0].strip())) if tdp_raw else 0
    except Exception:
        tdp = 0
    details.append(f"tdp_w={tdp}")

    # ECC 기준값 스냅샷 (부하 전)
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

    # 스트레스 도구 탐색 및 실행
    tool = "none"
    stress_proc = None

    # 1) gpu_burn 바이너리
    burn_bin = None
    if run("command -v gpu_burn"):
        burn_bin = "gpu_burn"
    elif os.path.isfile(f"{GPU_BURN_DIR}/gpu_burn") and os.access(f"{GPU_BURN_DIR}/gpu_burn", os.X_OK):
        burn_bin = f"{GPU_BURN_DIR}/gpu_burn"

    if burn_bin:
        tool = "gpu_burn"
        try:
            stress_proc = subprocess.Popen(
                [burn_bin, str(duration)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"gpu_burn launch failed: {e}", file=sys.stderr)
            stress_proc = None
            tool = "none"

    # 2) nvcc → 소스 빌드
    if tool == "none" and run("command -v nvcc") and run("command -v git") and run("command -v make"):
        print("nvcc found — building gpu_burn from source", file=sys.stderr)
        subprocess.run(f"rm -rf {GPU_BURN_DIR}", shell=True)
        r = subprocess.run(
            f"git clone --depth=1 https://github.com/wilicc/gpu-burn.git {GPU_BURN_DIR} "
            f"&& make -C {GPU_BURN_DIR}",
            shell=True, capture_output=True, timeout=300,
        )
        built = os.path.isfile(f"{GPU_BURN_DIR}/gpu_burn") and os.access(f"{GPU_BURN_DIR}/gpu_burn", os.X_OK)
        if built:
            tool = "gpu_burn"
            try:
                stress_proc = subprocess.Popen(
                    [f"{GPU_BURN_DIR}/gpu_burn", str(duration)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                tool = "none"
                stress_proc = None

    # 3) dcgmi
    if tool == "none" and run("command -v dcgmi"):
        tool = "dcgmi"
        try:
            stress_proc = subprocess.Popen(
                ["dcgmi", "diag", "-r", "2"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            tool = "none"
            stress_proc = None

    # 4) PyTorch
    if tool == "none":
        torch_ok, rc = run_rc(
            "python3 -c \"import torch; assert torch.cuda.is_available()\"", timeout=15
        )
        if rc == 0:
            tool = "pytorch"
            py_script = (
                f"import time, torch\n"
                f"n=8192\n"
                f"devs=[torch.device(f'cuda:{{i}}') for i in range(torch.cuda.device_count())]\n"
                f"pairs=[(torch.randn(n,n,device=d),torch.randn(n,n,device=d)) for d in devs]\n"
                f"end_t=time.time()+{duration}\n"
                f"[torch.matmul(a,b) for a,b in pairs for _ in iter(lambda: time.time()<end_t, False)]\n"
            )
            try:
                stress_proc = subprocess.Popen(
                    ["python3", "-c", py_script],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                tool = "none"
                stress_proc = None

    if tool == "none":
        if status == "pass":
            status = "warn"
        details.append("WARN:no_stress_tool_temp_only")

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
        # stress 프로세스 생존 확인
        if stress_proc is not None and stress_proc.poll() is not None:
            print(f"stress tool exited early (pid={stress_proc.pid})", file=sys.stderr)
            stress_died = True
            break

        # GPU별 메트릭 수집
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
            _, temp_s, power_s, util_s, throttle_s = parts[0], parts[1], parts[2], parts[3], parts[4]

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

    # stress 정리
    if stress_proc is not None and stress_proc.poll() is None:
        stress_proc.terminate()
        try:
            stress_proc.wait(timeout=5)
        except Exception:
            stress_proc.kill()

    # ECC 사후 측정
    ecc_corr_after = read_ecc("ecc.errors.corrected.volatile.total")
    ecc_uncorr_after = read_ecc("ecc.errors.uncorrected.volatile.total")
    ecc_delta_corr = max(0, ecc_corr_after - ecc_corr_before)
    ecc_delta_uncorr = max(0, ecc_uncorr_after - ecc_uncorr_before)

    # 평균 Utilization
    avg_util = util_sum // sample_count if sample_count > 0 else 0

    # 전력 비율
    pwr_ratio = int(peak_power / tdp * 100) if tdp > 0 else 0

    details += [
        f"tool={tool}",
        f"duration_s={duration}",
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
    if tool != "none" and avg_util < 80 and sample_count > 0:
        if status == "pass":
            status = "warn"
        details.append(f"WARN:low_gpu_utilization_avg={avg_util}pct")
    if stress_died:
        if status == "pass":
            status = "warn"
        details.append("WARN:stress_tool_exited_early")

    emit(status, details)


if __name__ == "__main__":
    main()

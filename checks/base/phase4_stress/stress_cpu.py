#!/usr/bin/env python3
"""stress_cpu — CPU 스트레스 테스트
Phase4: 부하 중 온도/주파수/Utilization 모니터링

환경변수:
  CPU_BURNIN_DURATION  부하 지속 시간(초) [기본: 120]

FAIL: peak_temp > 100°C
WARN: SW throttle(주파수 강하) | 도구 없음 | util < 80%
출력: {"check":"stress_cpu","status":"pass|fail|warn","detail":"..."}
"""
import json
import os
import subprocess
import sys
import threading
import time

CHECK = "stress_cpu"


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def emit(status, details):
    print(json.dumps({"check": CHECK, "status": status, "detail": "|".join(details)}, ensure_ascii=False))
    sys.exit(0)


def read_proc_stat():
    """Return (user, nice, system, idle, iowait, irq, softirq) from /proc/stat first line."""
    try:
        line = open("/proc/stat").readline()
        parts = line.split()
        return tuple(int(p) for p in parts[1:8])
    except Exception:
        return None


def main():
    status = "pass"
    details = []
    duration = int(os.environ.get("CPU_BURNIN_DURATION", "120"))
    nproc = int(run("nproc") or "1")
    details += [f"logical_cpus={nproc}", f"duration_s={duration}"]

    # 스트레스 도구 탐색
    tool = "none"
    stress_proc = None

    if run("command -v stress-ng"):
        tool = "stress-ng"
        try:
            stress_proc = subprocess.Popen(
                ["stress-ng", "--cpu", str(nproc), "--cpu-method", "matrixprod",
                 "--timeout", f"{duration}s"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            tool = "none"
            stress_proc = None

    if tool == "none" and run("command -v stress"):
        tool = "stress"
        try:
            stress_proc = subprocess.Popen(
                ["stress", "--cpu", str(nproc), "--timeout", str(duration)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            tool = "none"
            stress_proc = None

    if tool == "none":
        print("stress/stress-ng not found — falling back to python3", file=sys.stderr)
        tool = "python3"
        stop_event = threading.Event()

        def burn():
            while not stop_event.is_set():
                sum(i * i for i in range(10000))

        threads = [threading.Thread(target=burn, daemon=True) for _ in range(nproc)]
        for t in threads:
            t.start()

        # python3 fallback은 별도 스레드로 처리; stress_proc은 None 유지

    # 온도 파일 목록 결정
    temp_files = []
    from pathlib import Path
    thermal_base = Path("/sys/class/thermal")
    if thermal_base.exists():
        for zone in sorted(thermal_base.glob("thermal_zone*")):
            type_file = zone / "type"
            temp_file = zone / "temp"
            if not (type_file.exists() and temp_file.exists()):
                continue
            zone_type = type_file.read_text().strip()
            if any(k in zone_type for k in ("x86_pkg_temp", "acpitz", "cpu")):
                temp_files.append(temp_file)

    # 모니터링 루프 (5초 간격)
    peak_temp = 0
    min_freq_mhz = 999999
    util_sum = 0
    sample_count = 0
    throttle_count = 0
    stress_died = False

    end_time = time.time() + duration

    # 최대 주파수 (한 번만 읽기)
    max_freq_khz = 0
    try:
        max_freq_khz = int(open("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq").read().strip())
    except Exception:
        pass

    while time.time() < end_time:
        # stress 프로세스 생존 확인
        if stress_proc is not None and stress_proc.poll() is not None:
            print(f"stress tool exited early (pid={stress_proc.pid})", file=sys.stderr)
            stress_died = True
            break

        # CPU 온도 수집
        for f in temp_files:
            try:
                raw = int(f.read_text().strip())
                temp_c = raw // 1000
                if temp_c > peak_temp:
                    peak_temp = temp_c
            except Exception:
                pass

        # sensors 백업
        if not temp_files:
            sens_out = run("sensors 2>/dev/null | grep -oP 'Package id \\d+:\\s+\\+\\K[0-9.]+'")
            if sens_out:
                try:
                    temp_c = int(float(sens_out.splitlines()[-1]))
                    if temp_c > peak_temp:
                        peak_temp = temp_c
                except Exception:
                    pass

        # 현재 주파수 (cpu0)
        try:
            freq_khz = int(open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").read().strip())
            freq_mhz = freq_khz // 1000
            if freq_mhz < min_freq_mhz:
                min_freq_mhz = freq_mhz
            if max_freq_khz > 0:
                ratio = freq_khz * 100 // max_freq_khz
                if ratio < 80:
                    throttle_count += 1
        except Exception:
            pass

        # CPU Utilization (/proc/stat 두 번 읽기, 1초 간격)
        stat1 = read_proc_stat()
        if stat1:
            time.sleep(1)
            stat2 = read_proc_stat()
            if stat2:
                idle1 = stat1[3]
                idle2 = stat2[3]
                total1 = sum(stat1)
                total2 = sum(stat2)
                d_idle = idle2 - idle1
                d_total = total2 - total1
                if d_total > 0:
                    util = int((1 - d_idle / d_total) * 100)
                    util_sum += util
                    sample_count += 1
                time.sleep(4)
            else:
                time.sleep(5)
        else:
            time.sleep(5)

    # stress 정리
    if tool == "python3":
        stop_event.set()
        for t in threads:
            t.join(timeout=2)
    elif stress_proc is not None and stress_proc.poll() is None:
        stress_proc.terminate()
        try:
            stress_proc.wait(timeout=5)
        except Exception:
            stress_proc.kill()

    # 평균 Utilization
    avg_util = util_sum // sample_count if sample_count > 0 else 0

    # 최소 주파수 정리
    if min_freq_mhz == 999999:
        min_freq_mhz = 0

    # 최대 주파수 (MHz)
    max_freq_mhz = max_freq_khz // 1000 if max_freq_khz > 0 else 0

    details += [
        f"tool={tool}",
        f"peak_temp_c={peak_temp}",
        f"max_freq_mhz={max_freq_mhz}",
        f"min_freq_mhz_under_load={min_freq_mhz}",
        f"avg_util_pct={avg_util}",
        f"throttle_sample_count={throttle_count}",
    ]

    # FAIL 판정
    if peak_temp > 100:
        status = "fail"
        details.append(f"FAIL:peak_temp_over_100c({peak_temp}c)")

    # WARN 판정
    if throttle_count > 0:
        if status == "pass":
            status = "warn"
        details.append(f"WARN:freq_throttle_detected_{throttle_count}_samples")
    if tool != "none" and avg_util < 80 and sample_count > 0:
        if status == "pass":
            status = "warn"
        details.append(f"WARN:low_cpu_utilization_avg={avg_util}pct")
    if stress_died:
        if status == "pass":
            status = "warn"
        details.append("WARN:stress_tool_exited_early")

    emit(status, details)


if __name__ == "__main__":
    main()

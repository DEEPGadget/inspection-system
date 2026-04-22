#!/usr/bin/env python3
"""stress_cpu — CPU 스트레스 테스트
Phase4: 부하 중 온도/주파수/Utilization 모니터링 + 시계열 로깅

환경변수:
  CPU_BURNIN_DURATION       부하 지속 시간(초) [기본: 120]
  STRESS_SAMPLE_INTERVAL    샘플링 간격(초)   [기본: 5]

FAIL: peak_temp > 100°C
WARN: SW throttle(주파수 강하) | 도구 없음 | util < 80% | 온도 센서 미탐지
출력: stdout JSON 한 줄 {"check":..., "status":..., "detail":..., "timeseries":{...}}
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


def emit(status, details, timeseries=None):
    result = {"check": CHECK, "status": status, "detail": "|".join(details)}
    if timeseries is not None:
        result["timeseries"] = timeseries
    print(json.dumps(result, ensure_ascii=False))
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
    sample_interval = max(1, int(os.environ.get("STRESS_SAMPLE_INTERVAL", "5")))
    nproc = int(run("nproc") or "1")
    details += [
        f"logical_cpus={nproc}",
        f"duration_s={duration}",
        f"sample_interval_s={sample_interval}",
    ]

    # 스트레스 도구 탐색
    tool = "none"
    stress_proc = None

    if run("command -v stress-ng"):
        tool = "stress-ng"
        try:
            stress_proc = subprocess.Popen(
                [
                    "stress-ng",
                    "--cpu",
                    str(nproc),
                    "--cpu-method",
                    "matrixprod",
                    "--timeout",
                    f"{duration}s",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            tool = "none"
            stress_proc = None

    if tool == "none" and run("command -v stress"):
        tool = "stress"
        try:
            stress_proc = subprocess.Popen(
                ["stress", "--cpu", str(nproc), "--timeout", str(duration)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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

    # 소켓별 온도 센서 수집.
    # 1 hwmon (coretemp/k10temp/zenpower) = 1 소켓.
    # 각 소켓 내부에서는 Intel="Package id N" / AMD="Tctl|Tdie" 1개만 채택.
    # AMD Tccd*, Intel "Core N"은 per-CCD/per-core 노이즈라 제외.
    # 멀티 소켓(EPYC dual, Xeon dual) 시 hwmon이 소켓 수만큼 등장.
    from pathlib import Path

    sockets: list[dict] = []  # [{"id": 0, "chip": "k10temp", "label": "Tctl", "file": Path}]

    hwmon_base = Path("/sys/class/hwmon")
    if hwmon_base.exists():
        for hwmon in sorted(hwmon_base.glob("hwmon*")):
            name_file = hwmon / "name"
            if not name_file.exists():
                continue
            chip = name_file.read_text().strip()
            if chip not in ("coretemp", "k10temp", "zenpower"):
                continue
            chosen: Path | None = None
            chosen_label = ""
            for temp_input in sorted(hwmon.glob("temp*_input")):
                label_file = Path(str(temp_input).replace("_input", "_label"))
                label = label_file.read_text().strip() if label_file.exists() else ""
                if chip in ("k10temp", "zenpower"):
                    if label and label not in ("Tctl", "Tdie"):
                        continue
                elif chip == "coretemp":
                    if label and not label.startswith("Package"):
                        continue
                chosen = temp_input
                chosen_label = label
                break  # 한 hwmon(소켓)당 1개만
            if chosen is not None:
                sockets.append(
                    {
                        "id": len(sockets),
                        "chip": chip,
                        "label": chosen_label,
                        "file": chosen,
                    }
                )

    # hwmon 못 찾으면 thermal_zone fallback (단일 소켓 처리)
    if not sockets:
        thermal_base = Path("/sys/class/thermal")
        if thermal_base.exists():
            for zone in sorted(thermal_base.glob("thermal_zone*")):
                type_file = zone / "type"
                temp_file = zone / "temp"
                if not (type_file.exists() and temp_file.exists()):
                    continue
                zone_type = type_file.read_text().strip()
                if any(k in zone_type for k in ("x86_pkg_temp", "acpitz", "cpu")):
                    sockets.append(
                        {
                            "id": len(sockets),
                            "chip": zone_type,
                            "label": "",
                            "file": temp_file,
                        }
                    )

    # 모니터링 루프
    peak_temp = 0
    min_freq_mhz = 999999
    util_sum = 0
    sample_count = 0
    throttle_count = 0
    stress_died = False
    timeseries_samples: list[dict] = []

    start_time = time.time()
    end_time = start_time + duration

    # 최대 주파수 (한 번만 읽기)
    max_freq_khz = 0
    try:
        max_freq_khz = int(
            open("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq").read().strip()
        )
    except Exception:
        pass

    # util 측정을 위한 1초 대기를 제외한 순수 sleep 시간
    remaining_sleep = max(0, sample_interval - 1)

    while time.time() < end_time:
        # stress 프로세스 생존 확인
        if stress_proc is not None and stress_proc.poll() is not None:
            print(f"stress tool exited early (pid={stress_proc.pid})", file=sys.stderr)
            stress_died = True
            break

        # 이 샘플의 순간값
        sample_temp: int | None = None  # max across sockets (하위 호환)
        sample_freq: int | None = None
        sample_util: int | None = None
        socket_temps: list[int | None] = [None] * len(sockets)

        # 소켓별 CPU 온도 수집 (Tctl/Package id 0)
        for sock in sockets:
            try:
                raw = int(sock["file"].read_text().strip())
                temp_c = raw // 1000
                socket_temps[sock["id"]] = temp_c
                if sample_temp is None or temp_c > sample_temp:
                    sample_temp = temp_c
                if temp_c > peak_temp:
                    peak_temp = temp_c
            except Exception:
                pass

        # sensors 백업 (소켓 식별 불가 — 단일 값으로만 처리)
        if not sockets:
            sens_out = run(
                "sensors 2>/dev/null | grep -oP '(?:Package id \\d+|Tctl|Tdie):\\s+\\+\\K[0-9.]+'"
            )
            if sens_out:
                try:
                    temp_c = int(float(sens_out.splitlines()[-1]))
                    if sample_temp is None or temp_c > sample_temp:
                        sample_temp = temp_c
                    if temp_c > peak_temp:
                        peak_temp = temp_c
                except Exception:
                    pass

        # 현재 주파수 (cpu0)
        try:
            freq_khz = int(
                open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").read().strip()
            )
            freq_mhz = freq_khz // 1000
            sample_freq = freq_mhz
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
                    sample_util = util
                    util_sum += util
                    sample_count += 1
                time.sleep(remaining_sleep)
            else:
                time.sleep(sample_interval)
        else:
            time.sleep(sample_interval)

        # 시계열 샘플 기록 (socket_temps는 소켓별 온도 배열)
        timeseries_samples.append(
            {
                "t": int(time.time() - start_time),
                "temp": sample_temp,
                "socket_temps": socket_temps,
                "freq": sample_freq,
                "util": sample_util,
            }
        )

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

    # peak_temp_c=0은 "측정 실패"로 간주 (부하 중 CPU가 0°C일 리 없음).
    # 측정 불가 구분 위해 숫자 대신 'unknown' 표기.
    peak_temp_str = "unknown" if peak_temp == 0 else str(peak_temp)

    details += [
        f"tool={tool}",
        f"socket_count={len(sockets)}",
        f"peak_temp_c={peak_temp_str}",
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
    if peak_temp == 0:
        # 온도 센서 못 찾음 (hwmon 없음 + sensors fallback도 실패)
        if status == "pass":
            status = "warn"
        details.append("WARN:no_temp_sensor_detected")
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

    timeseries = {
        "sample_interval_s": sample_interval,
        "sockets": [{"id": s["id"], "chip": s["chip"], "label": s["label"]} for s in sockets],
        "samples": timeseries_samples,
    }
    emit(status, details, timeseries=timeseries)


if __name__ == "__main__":
    main()

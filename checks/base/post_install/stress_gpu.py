#!/usr/bin/env python3
"""stress_gpu — GPU 스트레스 테스트 (gadget-burn 전용)
Phase4: 부하 중 온도/전력/Utilization/Slowdown/ECC 모니터링 + 시계열 로깅

기대 동작:
  1. ~/gadget-burn/gadget_burn 바이너리 확인
  2. 없으면 git clone → make 빌드 (~ 하위, sudo 불필요, native 모드)
  3. `gadget_burn -t <duration>` 실행 (GPU 클래스 무관 공통 CLI)
  4. 빌드/실행 실패 시 사유(stderr 마지막 5줄)와 함께 fail 반환

환경변수:
  GPU_BURNIN_DURATION       부하 지속 시간(초) [기본: 300]
  STRESS_SAMPLE_INTERVAL    샘플링 간격(초)   [기본: 5]

FAIL: peak_temp > 87°C | HW throttle | ECC uncorrected 증가
      풀로드(util≥80%) + 저전력(power_ratio<70%)
      gadget-burn 확보/실행 실패
      cooling_consistency_score < 60 (수냉 불량)
WARN: SW/PWR throttle | ECC corrected 증가 | util<80%
      cooling_consistency_score < 75

출력: stdout JSON 한 줄 {"check":..., "status":..., "detail":..., "timeseries":{...}}
"""

import json
import os
import re
import statistics
import subprocess
import sys
import time

CHECK = "stress_gpu"
GADGET_BURN_DIR = os.path.expanduser("~/gadget-burn")
GADGET_BURN_BIN = f"{GADGET_BURN_DIR}/gadget_burn"
GADGET_BURN_REPO = "https://github.com/DEEPGadget/gadget-burn.git"


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


def emit(status, details, timeseries=None):
    """결과 JSON 출력 후 종료. timeseries가 있으면 포함."""
    result = {"check": CHECK, "status": status, "detail": "|".join(details)}
    if timeseries is not None:
        result["timeseries"] = timeseries
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0)


def classify_gpu(gpu_names: list[str]) -> str:
    """GPU 이름 목록으로 클래스 판별.
    GeForce 1장이라도 포함 → gaming (보수적: tensor core 경로 회피).
    전부 데이터센터/프로급 → datacenter.
    """
    if not gpu_names:
        return "unknown"
    for name in gpu_names:
        name_lower = name.lower()
        if "geforce" in name_lower or "titan" in name_lower:
            return "gaming"
    return "datacenter"


def collect_gpu_meta(gpu_count: int) -> list[dict]:
    """각 GPU의 고정 정보 수집 (테스트 시작 시 1회).

    반환: [{"index":0, "name":..., "power_cap_w":450, "vram_total_mb":24564,
            "slowdown_temp_c":98|None}, ...]
    slowdown_temp_c는 nvidia-smi -q -d TEMPERATURE에서 파싱 시도.
    """
    # CSV 쿼리로 index/name/power.max_limit/memory.total 수집
    out = run(
        "nvidia-smi --query-gpu=index,name,power.max_limit,memory.total "
        "--format=csv,noheader,nounits",
        timeout=10,
    )
    meta: list[dict] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            idx = int(parts[0])
            name = parts[1]
            power_cap = int(float(parts[2])) if parts[2] not in ("", "[N/A]") else 0
            vram_mb = int(float(parts[3])) if parts[3] not in ("", "[N/A]") else 0
        except Exception:
            continue
        meta.append(
            {
                "index": idx,
                "name": name,
                "power_cap_w": power_cap,
                "vram_total_mb": vram_mb,
                "slowdown_temp_c": None,
            }
        )

    # slowdown 온도 파싱 (GPU별 블록에서 "GPU Slowdown Temp" 또는 "Slowdown Temp")
    temp_out = run("nvidia-smi -q -d TEMPERATURE", timeout=10)
    slowdowns = _parse_slowdown_temps(temp_out)
    for i, slow in enumerate(slowdowns):
        if i < len(meta) and slow is not None:
            meta[i]["slowdown_temp_c"] = slow

    # GPU count 대비 부족하면 index만으로 패딩
    while len(meta) < gpu_count:
        meta.append(
            {
                "index": len(meta),
                "name": "unknown",
                "power_cap_w": 0,
                "vram_total_mb": 0,
                "slowdown_temp_c": None,
            }
        )
    return meta


def _parse_slowdown_temps(temp_out: str) -> list[int | None]:
    """nvidia-smi -q -d TEMPERATURE 출력에서 GPU별 slowdown 온도 추출.

    출력은 'GPU 00000000:XX:00.0' 블록으로 나뉘며, 각 블록에
    'GPU Slowdown Temp : 98 C' 같은 라인 포함. 없으면 None.
    """
    results: list[int | None] = []
    current: int | None = None
    found_in_block = False
    for raw in temp_out.splitlines():
        line = raw.strip()
        if line.startswith("GPU ") and ":" in line and "Temp" not in line:
            # 새 GPU 블록 시작
            if found_in_block:
                results.append(current)
            else:
                if current is not None or results:
                    results.append(None)
            current = None
            found_in_block = False
            continue
        # "GPU Slowdown Temp : 98 C" 또는 "GPU Slowdown T.Limit : 98 C"
        m = re.match(r"GPU\s+Slowdown\s+Temp\s*:\s*(\d+)", line)
        if m:
            # 정규식이 (\d+)로 숫자만 캡처하므로 int() 실패 불가
            current = int(m.group(1))
            found_in_block = True
    # 마지막 블록 flush
    if found_in_block:
        results.append(current)
    return results


def ensure_gadget_burn(details: list[str]) -> str | None:
    """gadget-burn 바이너리 경로 반환. 확보 실패 시 details에 사유 추가하고 None 반환."""
    # 1) 이미 있으면 그대로 사용
    if os.path.isfile(GADGET_BURN_BIN) and os.access(GADGET_BURN_BIN, os.X_OK):
        return GADGET_BURN_BIN

    # 2) 빌드 도구 점검
    missing = [t for t in ("nvcc", "git", "make") if not run(f"command -v {t}")]
    if missing:
        details.append(f"FAIL:gadget_burn_build_tools_missing={','.join(missing)}")
        return None

    # 3) 기존 디렉토리(부분 빌드 등) 정리
    subprocess.run(f"rm -rf {GADGET_BURN_DIR}", shell=True)

    # 4) git clone
    rc, _, err = run_full(f"git clone --depth=1 {GADGET_BURN_REPO} {GADGET_BURN_DIR}", timeout=120)
    if rc != 0:
        details.append(f"FAIL:gadget_burn_clone_failed:{tail_lines(err)}")
        return None

    # 5) make (native 모드)
    rc, _, err = run_full(f"make -C {GADGET_BURN_DIR}", timeout=300)
    if rc != 0:
        details.append(f"FAIL:gadget_burn_make_failed:{tail_lines(err)}")
        return None

    if os.path.isfile(GADGET_BURN_BIN) and os.access(GADGET_BURN_BIN, os.X_OK):
        return GADGET_BURN_BIN

    details.append("FAIL:gadget_burn_binary_missing_after_build")
    return None


def compute_cooling_consistency(
    samples: list[dict], gpu_count: int
) -> tuple[float, float, int, list[int], int]:
    """시계열 샘플로부터 냉각 일관성 지표 계산.

    Returns:
        avg_spread_c, max_spread_c, peak_spread_at_s, outlier_gpus, consistency_score
    """
    if not samples or gpu_count < 2:
        return 0.0, 0.0, 0, [], 100

    spreads = []
    peak_time = 0
    for s in samples:
        temps = [t for t in s.get("temp", []) if isinstance(t, (int, float)) and t > 0]
        if len(temps) < 2:
            continue
        spread = max(temps) - min(temps)
        spreads.append(spread)
        if spread == (max(spreads) if spreads else 0):
            peak_time = s.get("t", 0)

    if not spreads:
        return 0.0, 0.0, 0, [], 100

    avg_spread = statistics.mean(spreads)
    max_spread = max(spreads)

    # Outlier GPU 탐지: 각 GPU의 전체 기간 median 온도 기준
    per_gpu_medians: list[float] = []
    for gpu_i in range(gpu_count):
        gpu_temps = [
            s["temp"][gpu_i]
            for s in samples
            if gpu_i < len(s.get("temp", [])) and isinstance(s["temp"][gpu_i], (int, float))
        ]
        if gpu_temps:
            per_gpu_medians.append(statistics.median(gpu_temps))
        else:
            per_gpu_medians.append(0)

    outlier_gpus: list[int] = []
    if len(per_gpu_medians) >= 3:
        overall_median = statistics.median(per_gpu_medians)
        try:
            stdev = statistics.pstdev(per_gpu_medians)
        except statistics.StatisticsError:
            stdev = 0
        if stdev > 0:
            for gpu_i, median in enumerate(per_gpu_medians):
                if abs(median - overall_median) > 3 * stdev:
                    outlier_gpus.append(gpu_i)

    # consistency_score: 0~100 (편차 작을수록 고점)
    base = max(0, 90 - avg_spread * 8)
    peak_penalty = max(0, (max_spread - 5) * 2)
    outlier_penalty = len(outlier_gpus) * 10
    score = int(base - peak_penalty - outlier_penalty + 10)
    score = max(0, min(100, score))

    return avg_spread, max_spread, peak_time, outlier_gpus, score


def main():
    status = "pass"
    details = []
    duration = int(os.environ.get("GPU_BURNIN_DURATION", "300"))
    sample_interval = max(1, int(os.environ.get("STRESS_SAMPLE_INTERVAL", "5")))

    # nvidia-smi 확인
    out = run("nvidia-smi", timeout=10)
    if not out:
        emit("fail", ["FAIL:nvidia-smi not found"])

    # GPU 수량 + 모델명 수집
    gpu_names_raw = run("nvidia-smi --query-gpu=name --format=csv,noheader", timeout=10)
    gpu_name_list = [line.strip() for line in gpu_names_raw.splitlines() if line.strip()]
    gpu_count = len(gpu_name_list)
    if gpu_count == 0:
        emit("fail", ["FAIL:no GPUs detected"])
    details.append(f"gpu_count={gpu_count}")

    # GPU 메타 수집 (powercap, VRAM, slowdown temp)
    gpu_meta = collect_gpu_meta(gpu_count)

    # GPU 클래스 판별 (gaming vs datacenter)
    gpu_class = classify_gpu(gpu_name_list)
    details.append(f"gpu_class={gpu_class}")

    # TDP (peak_power_w 계산용; meta에서 첫 GPU 기준)
    tdp = gpu_meta[0]["power_cap_w"] if gpu_meta else 0
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

    # gadget-burn 확보 (없으면 빌드)
    burn_bin = ensure_gadget_burn(details)
    if burn_bin is None:
        details.append(f"tool=none|duration_s={duration}")
        emit("fail", details)

    tool = "gadget-burn"
    details.append(f"tool={tool}")
    details.append(f"duration_s={duration}")
    details.append(f"sample_interval_s={sample_interval}")

    # gadget-burn 실행 — gaming/datacenter 무관 공통 CLI (-t <duration>)
    burn_args = [burn_bin, "-t", str(duration)]
    try:
        stress_proc = subprocess.Popen(
            burn_args,
            cwd=GADGET_BURN_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as e:
        details.append(f"FAIL:gadget_burn_launch_failed:{e}")
        emit("fail", details)

    # 모니터링 루프 + 시계열 샘플 수집
    peak_temp = 0
    peak_power = 0
    util_sum = 0
    sample_count = 0
    slowdown_hw = 0
    slowdown_sw = 0
    slowdown_pwr = 0
    stress_died = False
    timeseries_samples: list[dict] = []

    start_time = time.time()
    end_time = start_time + duration

    while time.time() < end_time:
        if stress_proc.poll() is not None:
            stress_died = True
            break

        smi_out = run(
            "nvidia-smi --query-gpu=index,temperature.gpu,power.draw,"
            "utilization.gpu,memory.used,clocks_throttle_reasons.active "
            "--format=csv,noheader,nounits",
            timeout=10,
        )

        # 이번 샘플의 GPU별 값 배열 (index 순 보장)
        sample_temps: list[int | None] = [None] * gpu_count
        sample_powers: list[int | None] = [None] * gpu_count
        sample_utils: list[int | None] = [None] * gpu_count
        sample_mem: list[int | None] = [None] * gpu_count

        for line in smi_out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                continue
            idx_s, temp_s, power_s, util_s, mem_s, throttle_s = parts[:6]

            try:
                idx = int(idx_s)
            except Exception:
                continue
            if idx >= gpu_count:
                continue

            # nvidia-smi 필드는 드물게 "[N/A]"를 반환해 int() 변환이 실패할 수 있음.
            # 베스트에포트 텔레메트리 수집이므로 파싱 실패한 샘플은 무시하고 다음 필드로 진행.
            try:
                temp = int(temp_s)
                sample_temps[idx] = temp
                if temp > peak_temp:
                    peak_temp = temp
            except (ValueError, TypeError):
                pass  # [N/A] 등 비정상 문자열 시 해당 샘플 온도값 무시 (베스트에포트)
            try:
                pwr = int(float(power_s))
                sample_powers[idx] = pwr
                if pwr > peak_power:
                    peak_power = pwr
            except (ValueError, TypeError):
                pass  # [N/A] 등 비정상 문자열 시 해당 샘플 전력값 무시 (베스트에포트)
            try:
                util = int(util_s)
                sample_utils[idx] = util
                util_sum += util
                sample_count += 1
            except (ValueError, TypeError):
                pass  # [N/A] 등 비정상 문자열 시 해당 샘플 GPU 사용률 무시 (베스트에포트)
            try:
                mem_used = int(float(mem_s))
                sample_mem[idx] = mem_used
            except (ValueError, TypeError):
                pass  # [N/A] 등 비정상 문자열 시 해당 샘플 메모리값 무시 (베스트에포트)

            if throttle_s.startswith("0x"):
                try:
                    dec = int(throttle_s, 16)
                    if dec & 0x8:
                        slowdown_hw += 1
                    if dec & 0x4:
                        slowdown_sw += 1
                    if dec & 0x1:
                        slowdown_pwr += 1
                except (ValueError, TypeError):
                    pass  # [N/A] 등 비정상 문자열 시 해당 샘플 throttle 비트 무시 (베스트에포트)

        # 시계열 샘플 기록 (상대 시간 초 단위)
        timeseries_samples.append(
            {
                "t": int(time.time() - start_time),
                "temp": sample_temps,
                "power": sample_powers,
                "util": sample_utils,
                "mem_used_mb": sample_mem,
            }
        )

        time.sleep(sample_interval)

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

    # 냉각 일관성 지표 계산
    (
        cooling_avg_spread,
        cooling_max_spread,
        cooling_peak_at,
        cooling_outliers,
        cooling_score,
    ) = compute_cooling_consistency(timeseries_samples, gpu_count)

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
        f"cooling_avg_spread_c={cooling_avg_spread:.1f}",
        f"cooling_max_spread_c={cooling_max_spread:.1f}",
        f"cooling_peak_spread_at_s={cooling_peak_at}",
        f"cooling_outlier_gpus={','.join(str(g) for g in cooling_outliers) if cooling_outliers else 'none'}",
        f"cooling_consistency_score={cooling_score}",
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
        details.append(f"FAIL:gadget_burn_exited_early:{tail_lines(burn_stderr)}")
    if cooling_score < 60 and gpu_count >= 2:
        status = "fail"
        details.append(
            f"FAIL:cooling_inconsistency_score_below_60(score={cooling_score},"
            f"avg_spread={cooling_avg_spread:.1f}c,outliers={len(cooling_outliers)})"
        )

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
    if 60 <= cooling_score < 75 and gpu_count >= 2:
        if status == "pass":
            status = "warn"
        details.append(f"WARN:cooling_inconsistency_score_below_75(score={cooling_score})")

    # 시계열 메타 + 샘플 조합
    timeseries = {
        "sample_interval_s": sample_interval,
        "gpus": gpu_meta,
        "samples": timeseries_samples,
    }

    emit(status, details, timeseries=timeseries)


if __name__ == "__main__":
    main()

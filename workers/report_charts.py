"""리포트용 matplotlib 차트 생성.

stress_gpu / stress_cpu / nccl_bandwidth 의 시계열 및 집계 결과를 PNG로 저장.
workers/report.py 에서 호출.
"""

from __future__ import annotations

import colorsys
import json
import statistics
from pathlib import Path

# noninteractive backend (서버 환경, X11 없음)
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import to_rgb  # noqa: E402

_BASE_COLORS = ["tab:blue", "tab:red", "tab:green", "tab:purple", "tab:orange"]


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _shade(base_color: str, idx: int, total: int):
    """같은 base_color 계열 내에서 idx/total 비율로 lightness 조정한 색상 반환."""
    r, g, b = to_rgb(base_color)
    h, light, sat = colorsys.rgb_to_hls(r, g, b)
    if total <= 1:
        new_light = light
    else:
        # 0.35 ~ 0.75 범위에 분포
        new_light = 0.35 + 0.4 * idx / (total - 1)
    return colorsys.hls_to_rgb(h, new_light, sat)


def _group_gpu_colors(gpus: list[dict]) -> dict[int, tuple[float, float, float]]:
    """GPU index → RGB 색상. 같은 name은 같은 base color의 shade로 구분."""
    groups: dict[str, list[int]] = {}
    for g in gpus:
        groups.setdefault(g.get("name", "unknown"), []).append(g["index"])

    colors: dict[int, tuple[float, float, float]] = {}
    for group_i, (_name, indices) in enumerate(groups.items()):
        base = _BASE_COLORS[group_i % len(_BASE_COLORS)]
        for j, idx in enumerate(sorted(indices)):
            colors[idx] = _shade(base, j, len(indices))
    return colors


def _gpu_type_label(gpus: list[dict]) -> str:
    """GPU 종류 요약 라벨. 같은 종류면 'RTX 4090 ×4', 혼종이면 'RTX 4090 ×2 + A100 ×2'."""
    groups: dict[str, int] = {}
    for g in gpus:
        name = g.get("name", "unknown")
        groups[name] = groups.get(name, 0) + 1
    parts = [f"{name} ×{cnt}" for name, cnt in groups.items()]
    return " + ".join(parts)


# ---------------------------------------------------------------------------
# stress_gpu chart
# ---------------------------------------------------------------------------


def generate_gpu_chart(
    timeseries_path: Path,
    output_path: Path,
    user_temp_limit: float | None = 87.0,
) -> bool:
    """GPU 스트레스 시계열 차트 생성 (4 panel).

    Returns True if chart was written, False if no data.
    """
    data = _load_json(timeseries_path)
    if not data or not data.get("samples"):
        return False

    gpus: list[dict] = data.get("gpus", [])
    samples: list[dict] = data["samples"]
    if not gpus or not samples:
        return False

    gpu_count = len(gpus)
    colors = _group_gpu_colors(gpus)

    times = [s.get("t", i) for i, s in enumerate(samples)]

    # GPU별 시계열 분리
    temps_per_gpu: list[list[float]] = [[] for _ in range(gpu_count)]
    powers_per_gpu: list[list[float]] = [[] for _ in range(gpu_count)]
    utils_per_gpu: list[list[float]] = [[] for _ in range(gpu_count)]
    mem_per_gpu: list[list[float]] = [[] for _ in range(gpu_count)]

    for s in samples:
        for i in range(gpu_count):
            t = _safe_num(
                s.get("temp", [None] * gpu_count)[i] if i < len(s.get("temp", [])) else None
            )
            p = _safe_num(
                s.get("power", [None] * gpu_count)[i] if i < len(s.get("power", [])) else None
            )
            u = _safe_num(
                s.get("util", [None] * gpu_count)[i] if i < len(s.get("util", [])) else None
            )
            m = _safe_num(
                s.get("mem_used_mb", [None] * gpu_count)[i]
                if i < len(s.get("mem_used_mb", []))
                else None
            )
            temps_per_gpu[i].append(t)
            powers_per_gpu[i].append(p)
            utils_per_gpu[i].append(u)
            mem_per_gpu[i].append(m)

    # 4-panel layout (2x2)
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    (ax_temp, ax_power), (ax_mem, ax_util) = axes

    title_suffix = f"  |  GPUs: {_gpu_type_label(gpus)}"

    # ── Panel 1: 온도 ─────────────────────────────────────────
    _plot_temp_panel(ax_temp, times, temps_per_gpu, gpus, colors, user_temp_limit)
    ax_temp.set_title(f"GPU Chip Temperature (°C){title_suffix}")

    # ── Panel 2: 전력 ─────────────────────────────────────────
    _plot_power_panel(ax_power, times, powers_per_gpu, gpus, colors)
    ax_power.set_title("GPU Power Draw (W)")

    # ── Panel 3: VRAM ─────────────────────────────────────────
    _plot_vram_panel(ax_mem, times, mem_per_gpu, gpus, colors)
    ax_mem.set_title("VRAM Used (MB)")

    # ── Panel 4: 전체 사용률 ──────────────────────────────────
    _plot_util_panel(ax_util, times, utils_per_gpu, gpus, colors)
    ax_util.set_title("GPU Utilization (%)")

    fig.tight_layout()
    fig.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)

    # 냉각 일관성 heatmap은 별도 figure
    heatmap_path = output_path.with_name(output_path.stem + "_cooling_heatmap" + output_path.suffix)
    _generate_cooling_heatmap(times, temps_per_gpu, gpus, heatmap_path)

    return True


def _safe_num(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return float("nan")


def _plot_temp_panel(ax, times, temps_per_gpu, gpus, colors, user_limit):
    gpu_count = len(gpus)
    # 7개 이상이면 envelope만, 그 이하면 라인 + envelope
    show_lines = gpu_count <= 6

    # envelope (min-max across GPUs at each t)
    min_t, max_t, median_t = [], [], []
    for ti in range(len(times)):
        vals = [
            temps_per_gpu[g][ti]
            for g in range(gpu_count)
            if ti < len(temps_per_gpu[g]) and _is_valid(temps_per_gpu[g][ti])
        ]
        if vals:
            min_t.append(min(vals))
            max_t.append(max(vals))
            median_t.append(statistics.median(vals))
        else:
            min_t.append(float("nan"))
            max_t.append(float("nan"))
            median_t.append(float("nan"))

    ax.fill_between(times, min_t, max_t, alpha=0.15, color="gray", label="min~max range")
    ax.plot(times, median_t, color="black", linewidth=1.5, linestyle="--", label="median")

    if show_lines:
        for i, g in enumerate(gpus):
            ax.plot(
                times,
                temps_per_gpu[i],
                color=colors[g["index"]],
                linewidth=1.0,
                label=f"GPU {g['index']}",
                alpha=0.8,
            )

    # 기준선
    if user_limit is not None:
        ax.axhline(
            user_limit,
            color="red",
            linestyle=":",
            linewidth=1.5,
            label=f"User limit ({user_limit:.0f}°C)",
        )
    # NVIDIA SW slowdown — 모든 GPU 중 min slowdown 표시 (가장 보수적)
    slowdown_temps = [g["slowdown_temp_c"] for g in gpus if g.get("slowdown_temp_c")]
    if slowdown_temps:
        sd = min(slowdown_temps)
        ax.axhline(
            sd,
            color="darkorange",
            linestyle=":",
            linewidth=1.5,
            label=f"SW slowdown ({sd:.0f}°C)",
        )

    # 최댓값 어노테이션
    _annotate_max_temp(ax, times, temps_per_gpu, gpus)

    # spread 통계 annotation
    spreads = [
        (max_t[i] - min_t[i])
        for i in range(len(times))
        if _is_valid(min_t[i]) and _is_valid(max_t[i])
    ]
    if spreads:
        avg_sp = statistics.mean(spreads)
        max_sp = max(spreads)
        ax.text(
            0.02,
            0.97,
            f"spread: avg {avg_sp:.1f}°C, peak {max_sp:.1f}°C",
            transform=ax.transAxes,
            va="top",
            fontsize=9,
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.8),
        )

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Temperature (°C)")
    ax.legend(loc="lower right", fontsize=8, ncol=2)
    ax.grid(True, alpha=0.3)


def _annotate_max_temp(ax, times, temps_per_gpu, gpus):
    max_val = float("-inf")
    max_gpu = -1
    max_t = -1
    for i, g in enumerate(gpus):
        for ti, v in enumerate(temps_per_gpu[i]):
            if _is_valid(v) and v > max_val:
                max_val = v
                max_gpu = g["index"]
                max_t = times[ti]
    if max_gpu < 0:
        return
    ax.annotate(
        f"max: {max_val:.0f}°C\nGPU {max_gpu} @ t={max_t}s",
        xy=(max_t, max_val),
        xytext=(10, 10),
        textcoords="offset points",
        fontsize=8,
        bbox=dict(boxstyle="round,pad=0.3", facecolor="yellow", alpha=0.7),
        arrowprops=dict(arrowstyle="->", color="black", lw=0.8),
    )


def _plot_power_panel(ax, times, powers_per_gpu, gpus, colors):
    for i, g in enumerate(gpus):
        ax.plot(
            times,
            powers_per_gpu[i],
            color=colors[g["index"]],
            linewidth=1.0,
            label=f"GPU {g['index']} ({g.get('power_cap_w', 0)}W cap)",
            alpha=0.8,
        )

    # GPU별 powercap 점선 (중복 제거)
    caps_seen: set[int] = set()
    for g in gpus:
        cap = g.get("power_cap_w", 0)
        if cap > 0 and cap not in caps_seen:
            ax.axhline(
                cap,
                color=colors[g["index"]],
                linestyle=":",
                linewidth=0.8,
                alpha=0.5,
            )
            caps_seen.add(cap)

    # y축 상한 = max(powercap) * 1.1
    max_cap = max((g.get("power_cap_w", 0) for g in gpus), default=0)
    if max_cap > 0:
        ax.set_ylim(0, max_cap * 1.1)

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Power (W)")
    ax.legend(loc="lower right", fontsize=7, ncol=max(1, len(gpus) // 8))
    ax.grid(True, alpha=0.3)


def _plot_vram_panel(ax, times, mem_per_gpu, gpus, colors):
    for i, g in enumerate(gpus):
        ax.plot(
            times,
            mem_per_gpu[i],
            color=colors[g["index"]],
            linewidth=1.0,
            label=f"GPU {g['index']}",
            alpha=0.8,
        )

    # y축 MAX = VRAM 용량 (동일 카드면 단일값, 혼종이면 최대값)
    max_vram = max((g.get("vram_total_mb", 0) for g in gpus), default=0)
    if max_vram > 0:
        ax.set_ylim(0, max_vram)
        ax.axhline(max_vram, color="gray", linestyle=":", linewidth=0.8, alpha=0.5)

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Memory Used (MB)")
    ax.legend(loc="lower right", fontsize=7, ncol=max(1, len(gpus) // 8))
    ax.grid(True, alpha=0.3)


def _plot_util_panel(ax, times, utils_per_gpu, gpus, colors):
    for i, g in enumerate(gpus):
        ax.plot(
            times,
            utils_per_gpu[i],
            color=colors[g["index"]],
            linewidth=1.0,
            label=f"GPU {g['index']}",
            alpha=0.8,
        )
    ax.set_ylim(0, 100)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Utilization (%)")
    ax.legend(loc="lower right", fontsize=7, ncol=max(1, len(gpus) // 8))
    ax.grid(True, alpha=0.3)


def _is_valid(v) -> bool:
    return isinstance(v, (int, float)) and v == v  # NaN check: NaN != NaN


# ---------------------------------------------------------------------------
# Cooling heatmap — GPU별 온도 추이 (x=시간, y=GPU)
# ---------------------------------------------------------------------------


def _generate_cooling_heatmap(times, temps_per_gpu, gpus, output_path: Path):
    """GPU index × 시간축 heatmap. 수냉 냉각 일관성 한눈에 확인."""
    if not gpus:
        return
    gpu_count = len(gpus)
    # matrix[gpu_i][t_i] = temp
    matrix = [temps_per_gpu[i] for i in range(gpu_count)]

    fig, ax = plt.subplots(figsize=(11, max(3, 0.4 * gpu_count + 1.5)))
    import numpy as np

    arr = np.array(matrix, dtype=float)
    im = ax.imshow(
        arr,
        aspect="auto",
        cmap="viridis",
        extent=[times[0] if times else 0, times[-1] if times else 1, gpu_count - 0.5, -0.5],
        interpolation="nearest",
    )
    cbar = fig.colorbar(im, ax=ax, label="Temperature (°C)")
    cbar.ax.tick_params(labelsize=8)

    ax.set_yticks(range(gpu_count))
    ax.set_yticklabels([f"GPU {g['index']}" for g in gpus], fontsize=8)
    ax.set_xlabel("Time (s)")
    ax.set_title("Cooling Consistency — Per-GPU Temperature Heatmap")
    ax.grid(False)
    fig.tight_layout()
    fig.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# stress_cpu chart
# ---------------------------------------------------------------------------


def generate_cpu_chart(timeseries_path: Path, output_path: Path) -> bool:
    data = _load_json(timeseries_path)
    if not data or not data.get("samples"):
        return False

    samples = data["samples"]
    times = [s.get("t", i) for i, s in enumerate(samples)]
    temps = [_safe_num(s.get("temp")) for s in samples]
    freqs = [_safe_num(s.get("freq")) for s in samples]
    utils = [_safe_num(s.get("util")) for s in samples]

    fig, axes = plt.subplots(3, 1, figsize=(11, 7), sharex=True)
    ax_t, ax_f, ax_u = axes

    ax_t.plot(times, temps, color="tab:red", linewidth=1.2, label="CPU Temp")
    ax_t.set_ylabel("Temperature (°C)")
    ax_t.set_title("CPU Stress Test")
    ax_t.grid(True, alpha=0.3)
    ax_t.legend(loc="lower right", fontsize=8)
    valid_temps = [t for t in temps if _is_valid(t)]
    if valid_temps:
        peak = max(valid_temps)
        peak_idx = temps.index(peak)
        ax_t.annotate(
            f"max: {peak:.0f}°C @ t={times[peak_idx]}s",
            xy=(times[peak_idx], peak),
            xytext=(10, 10),
            textcoords="offset points",
            fontsize=8,
            bbox=dict(boxstyle="round,pad=0.3", facecolor="yellow", alpha=0.7),
            arrowprops=dict(arrowstyle="->", color="black", lw=0.8),
        )

    ax_f.plot(times, freqs, color="tab:blue", linewidth=1.2, label="Frequency (cpu0)")
    ax_f.set_ylabel("Frequency (MHz)")
    ax_f.grid(True, alpha=0.3)
    ax_f.legend(loc="lower right", fontsize=8)

    ax_u.plot(times, utils, color="tab:green", linewidth=1.2, label="Utilization")
    ax_u.set_ylabel("Utilization (%)")
    ax_u.set_xlabel("Time (s)")
    ax_u.set_ylim(0, 100)
    ax_u.grid(True, alpha=0.3)
    ax_u.legend(loc="lower right", fontsize=8)

    fig.tight_layout()
    fig.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return True


# ---------------------------------------------------------------------------
# nccl_bandwidth bar chart
# ---------------------------------------------------------------------------


def generate_nccl_chart(detail_parsed: dict, output_path: Path) -> bool:
    """nccl_bandwidth의 실측 bw vs 기준선 bar chart. 시계열 아님."""
    try:
        bw2 = float(detail_parsed.get("bw_2gpu_gbs", "nan"))
        bw4 = float(detail_parsed.get("bw_4gpu_gbs", "nan"))
        min2 = float(detail_parsed.get("min_bw_2gpu_gbs", "nan"))
        min4 = float(detail_parsed.get("min_bw_4gpu_gbs", "nan"))
    except (ValueError, TypeError):
        return False

    measurements = []
    if _is_valid(bw2):
        measurements.append(("2 GPU", bw2, min2))
    if _is_valid(bw4):
        measurements.append(("4 GPU", bw4, min4))
    if not measurements:
        return False

    fig, ax = plt.subplots(figsize=(7, 4.5))
    labels = [m[0] for m in measurements]
    bws = [m[1] for m in measurements]
    mins = [m[2] for m in measurements]
    x = list(range(len(labels)))

    colors = []
    for b, m in zip(bws, mins):
        if _is_valid(m) and b >= m:
            colors.append("tab:green")
        else:
            colors.append("tab:red")
    bars = ax.bar(x, bws, color=colors, alpha=0.8, label="Measured")

    # 기준선 (각 bar 위에 점선)
    for xi, (b, m) in enumerate(zip(bws, mins)):
        if _is_valid(m):
            ax.hlines(
                m,
                xi - 0.4,
                xi + 0.4,
                colors="black",
                linestyles="--",
                linewidth=1.5,
                label="Threshold" if xi == 0 else "",
            )

    # 값 라벨
    for rect, val in zip(bars, bws):
        ax.text(
            rect.get_x() + rect.get_width() / 2,
            rect.get_height(),
            f"{val:.1f}",
            ha="center",
            va="bottom",
            fontsize=9,
        )

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Bandwidth (GB/s)")
    ax.set_title("NCCL All-Reduce Bandwidth vs Threshold")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return True

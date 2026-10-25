from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(r"C:\Users\Lenovo\Documents\BOOM")
BATCH_DIR = ROOT / "outputs" / "night_recovery" / "batch_1_42"
OUT_DIR = ROOT / "outputs" / "night_recovery" / "recovery_structure_v06"

TARGET_ALIASES = {
    "LiYou": {"liyou"},
    "HanShunyuan": {"hanshunyuan"},
    "WangYuhang": {"wangyuhang", "wyh"},
}

STATE_COLORS = {
    "sympathetic": "#D64545",
    "parasympathetic": "#2F80ED",
    "transition": "#A3A9B3",
    "invalid": "#E4E7EC",
}

SEGMENT_COLORS = {
    "rising": "#159947",
    "falling": "#E8751A",
    "stable": "#667085",
}


def normalize_user(value: object) -> str | None:
    raw = str(value).strip()
    key = raw.lower().replace(" ", "")
    for canonical, aliases in TARGET_ALIASES.items():
        if key in aliases:
            return canonical
    return None


def load_target_windows() -> pd.DataFrame:
    frames = []
    for day_dir in sorted(BATCH_DIR.glob("day*")):
        path = day_dir / "window_metrics.csv"
        if not path.exists():
            continue
        day = int(day_dir.name.replace("day", ""))
        df = pd.read_csv(path)
        df["day"] = day
        df["user"] = df["sheet"].map(normalize_user)
        df = df[df["user"].notna()].copy()
        if not df.empty:
            frames.append(df)
    if not frames:
        raise FileNotFoundError("No target user window_metrics.csv data found.")
    data = pd.concat(frames, ignore_index=True)
    data = data.sort_values(["user", "day", "window_mid_h"]).reset_index(drop=True)
    return data


def smooth_series(values: pd.Series, window: int, min_periods: int = 3) -> pd.Series:
    return values.astype(float).replace([np.inf, -np.inf], np.nan).rolling(
        window, center=True, min_periods=min_periods
    ).median()


def classify_lfhf(value: float) -> str:
    if not np.isfinite(value):
        return "invalid"
    if value >= 2.5:
        return "sympathetic"
    if value <= 1.5:
        return "parasympathetic"
    return "transition"


def state_episodes(group: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float]]:
    g = group.sort_values("window_mid_h").copy()
    g["lf_hf_smooth"] = smooth_series(g["lf_hf"], 9)
    g["state"] = g["lf_hf_smooth"].map(classify_lfhf)
    valid = g[g["state"] != "invalid"].copy()
    if valid.empty:
        return pd.DataFrame(), {}

    step_min = float(np.nanmedian(np.diff(valid["window_mid_h"].to_numpy()) * 60.0))
    if not np.isfinite(step_min) or step_min <= 0:
        step_min = 1.0

    episodes = []
    current_state = None
    start_idx = None
    prev_mid = None
    rows = valid.reset_index(drop=True)
    for idx, row in rows.iterrows():
        state = row["state"]
        mid = float(row["window_mid_h"])
        gap_break = prev_mid is not None and (mid - prev_mid) * 60.0 > step_min * 1.8
        if current_state is None or state != current_state or gap_break:
            if current_state is not None and start_idx is not None:
                sub = rows.iloc[start_idx:idx]
                episodes.append(build_state_episode(sub, current_state, step_min))
            current_state = state
            start_idx = idx
        prev_mid = mid
    if current_state is not None and start_idx is not None:
        episodes.append(build_state_episode(rows.iloc[start_idx:], current_state, step_min))

    ep_df = pd.DataFrame(episodes)
    total_count = len(valid)
    summary: dict[str, float] = {
        "autonomic_valid_windows": float(total_count),
        "autonomic_step_min": step_min,
        "autonomic_episode_count": float(len(ep_df)),
    }
    for state in ["sympathetic", "parasympathetic", "transition"]:
        count = int((valid["state"] == state).sum())
        state_eps = ep_df[ep_df["state"] == state]
        durations = state_eps["duration_min"].astype(float) if not state_eps.empty else pd.Series(dtype=float)
        summary[f"{state}_pct"] = count / total_count * 100.0
        summary[f"{state}_h"] = count * step_min / 60.0
        summary[f"{state}_episode_count"] = float(len(state_eps))
        summary[f"{state}_episode_mean_min"] = float(durations.mean()) if len(durations) else 0.0
        summary[f"{state}_episode_median_min"] = float(durations.median()) if len(durations) else 0.0
        summary[f"{state}_episode_p75_min"] = float(durations.quantile(0.75)) if len(durations) else 0.0
        summary[f"{state}_episode_max_min"] = float(durations.max()) if len(durations) else 0.0
    return ep_df, summary


def build_state_episode(rows: pd.DataFrame, state: str, step_min: float) -> dict[str, float | str]:
    return {
        "state": state,
        "start_h": float(rows["window_mid_h"].iloc[0] - step_min / 120.0),
        "end_h": float(rows["window_mid_h"].iloc[-1] + step_min / 120.0),
        "window_count": int(len(rows)),
        "duration_min": float(len(rows) * step_min),
        "lf_hf_median": float(rows["lf_hf_smooth"].median()),
    }


def segment_cost_arrays(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    n = len(x)
    sx = np.r_[0.0, np.cumsum(x)]
    sy = np.r_[0.0, np.cumsum(y)]
    sxx = np.r_[0.0, np.cumsum(x * x)]
    sxy = np.r_[0.0, np.cumsum(x * y)]
    syy = np.r_[0.0, np.cumsum(y * y)]
    cost = np.full((n + 1, n + 1), np.inf)
    for i in range(n):
        ends = np.arange(i + 2, n + 1)
        m = ends - i
        sum_x = sx[ends] - sx[i]
        sum_y = sy[ends] - sy[i]
        sum_xx = sxx[ends] - sxx[i]
        sum_xy = sxy[ends] - sxy[i]
        sum_yy = syy[ends] - syy[i]
        denom = m * sum_xx - sum_x * sum_x
        slope = np.divide(
            m * sum_xy - sum_x * sum_y,
            denom,
            out=np.zeros_like(sum_x, dtype=float),
            where=np.abs(denom) > 1e-12,
        )
        intercept = (sum_y - slope * sum_x) / m
        sse = sum_yy + slope * slope * sum_xx + m * intercept * intercept
        sse -= 2 * slope * sum_xy + 2 * intercept * sum_y - 2 * slope * intercept * sum_x
        cost[i, ends] = np.maximum(sse, 0.0)
    return cost


def piecewise_linear_segments(
    time_h: np.ndarray,
    y: np.ndarray,
    raw_rmssd: np.ndarray,
    min_duration_min: float = 40.0,
    max_segments: int = 4,
) -> tuple[pd.DataFrame, dict[str, float]]:
    mask = np.isfinite(time_h) & np.isfinite(y)
    time_h = time_h[mask]
    y = y[mask]
    raw_rmssd = raw_rmssd[mask]
    n = len(y)
    if n < 8:
        return pd.DataFrame(), {"rmssd_segment_valid_windows": float(n)}

    diffs_min = np.diff(time_h) * 60.0
    step_min = float(np.nanmedian(diffs_min)) if len(diffs_min) else 1.0
    if not np.isfinite(step_min) or step_min <= 0:
        step_min = 1.0
    min_pts = max(5, int(round(min_duration_min / step_min)))
    min_pts = min(min_pts, max(5, n // 2)) if n >= 10 else max(2, n)
    max_k = max(1, min(max_segments, n // min_pts))

    cost = segment_cost_arrays(time_h, y)
    dp = np.full((max_k + 1, n + 1), np.inf)
    prev = np.full((max_k + 1, n + 1), -1, dtype=int)
    dp[0, 0] = 0.0
    for k in range(1, max_k + 1):
        min_end = k * min_pts
        for end in range(min_end, n + 1):
            starts = np.arange((k - 1) * min_pts, end - min_pts + 1)
            vals = dp[k - 1, starts] + cost[starts, end]
            best_idx = int(np.argmin(vals))
            dp[k, end] = float(vals[best_idx])
            prev[k, end] = int(starts[best_idx])

    bics = []
    for k in range(1, max_k + 1):
        sse = dp[k, n]
        if not np.isfinite(sse):
            continue
        bic = n * math.log(sse / n + 1e-9) + k * 8.0 * math.log(n)
        bics.append((bic, k))
    best_k = min(bics)[1] if bics else 1

    bounds = []
    end = n
    for k in range(best_k, 0, -1):
        start = prev[k, end]
        if start < 0:
            start = 0
        bounds.append((start, end))
        end = start
    bounds.reverse()

    rows = []
    for idx, (start, end) in enumerate(bounds, 1):
        xs = time_h[start:end]
        ys = y[start:end]
        rr = raw_rmssd[start:end]
        slope, intercept = fit_line(xs, ys)
        fitted_start = slope * xs[0] + intercept
        fitted_end = slope * xs[-1] + intercept
        delta = fitted_end - fitted_start
        duration_h = max(float(xs[-1] - xs[0]), step_min / 60.0)
        trend = classify_rmssd_trend(delta, slope)
        residual = ys - (slope * xs + intercept)
        rows.append(
            {
                "segment_id": idx,
                "start_h": float(xs[0]),
                "end_h": float(xs[-1]),
                "duration_min": duration_h * 60.0,
                "trend_type": trend,
                "lnrmssd_start": float(ys[0]),
                "lnrmssd_end": float(ys[-1]),
                "lnrmssd_fitted_start": float(fitted_start),
                "lnrmssd_fitted_end": float(fitted_end),
                "lnrmssd_delta": float(delta),
                "lnrmssd_slope_per_h": float(slope),
                "segment_volatility": float(np.std(residual, ddof=1)) if len(residual) > 1 else 0.0,
                "segment_rmssd_cv": float(np.nanstd(rr, ddof=1) / np.nanmean(rr)) if len(rr) > 1 and np.nanmean(rr) else np.nan,
                "segment_sse": float(np.sum(residual * residual)),
                "window_count": int(end - start),
            }
        )

    seg_df = pd.DataFrame(rows)
    summary = summarize_rmssd_segments(seg_df, n, step_min)
    return seg_df, summary


def fit_line(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    if len(x) < 2 or np.allclose(x, x[0]):
        return 0.0, float(np.nanmean(y))
    slope, intercept = np.polyfit(x, y, 1)
    return float(slope), float(intercept)


def classify_rmssd_trend(delta: float, slope: float) -> str:
    if delta > 0.05 and slope > 0.03:
        return "rising"
    if delta < -0.05 and slope < -0.03:
        return "falling"
    return "stable"


def summarize_rmssd_segments(seg_df: pd.DataFrame, valid_windows: int, step_min: float) -> dict[str, float]:
    summary: dict[str, float] = {
        "rmssd_segment_valid_windows": float(valid_windows),
        "rmssd_segment_count": float(len(seg_df)),
        "rmssd_step_min": step_min,
    }
    total_min = float(seg_df["duration_min"].sum()) if not seg_df.empty else 0.0
    for trend in ["rising", "falling", "stable"]:
        part = seg_df[seg_df["trend_type"] == trend]
        dur = float(part["duration_min"].sum()) if not part.empty else 0.0
        summary[f"{trend}_stage_count"] = float(len(part))
        summary[f"total_{trend}_duration_pct"] = dur / total_min * 100.0 if total_min > 0 else 0.0
        summary[f"mean_{trend}_slope"] = float(part["lnrmssd_slope_per_h"].mean()) if not part.empty else 0.0
    rises = seg_df[seg_df["trend_type"] == "rising"]["lnrmssd_delta"]
    falls = seg_df[seg_df["trend_type"] == "falling"]["lnrmssd_delta"]
    summary["largest_rise_delta"] = float(rises.max()) if not rises.empty else 0.0
    summary["largest_fall_delta"] = float(falls.min()) if not falls.empty else 0.0
    summary["mean_segment_volatility"] = float(seg_df["segment_volatility"].mean()) if not seg_df.empty else np.nan
    summary["max_segment_volatility"] = float(seg_df["segment_volatility"].max()) if not seg_df.empty else np.nan
    return summary


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def draw_text(draw: ImageDraw.ImageDraw, xy, text: str, size: int = 22, fill="#1D2939", bold: bool = False, anchor=None):
    draw.text(xy, text, font=font(size, bold), fill=fill, anchor=anchor)


def plot_night(group: pd.DataFrame, state_windows: pd.DataFrame, seg_df: pd.DataFrame, out_path: Path) -> None:
    width, height = 1600, 980
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    user = group["user"].iloc[0]
    day = int(group["day"].iloc[0])
    draw_text(draw, (70, 42), f"{user} Day {day:02d} - Autonomic + RMSSD Structure", 34, "#101828", True)

    valid = group[group["valid_ratio"] >= 0.80].sort_values("window_mid_h").copy()
    x_min = float(valid["window_start_s"].min() / 3600.0) if not valid.empty else 0.0
    x_max = float(valid["window_end_s"].max() / 3600.0) if not valid.empty else 1.0
    if x_max <= x_min:
        x_max = x_min + 1.0

    plot_left, plot_right = 110, 1510
    ribbon_top, ribbon_bottom = 135, 205

    def sx(value: float) -> float:
        return plot_left + (value - x_min) / (x_max - x_min) * (plot_right - plot_left)

    # Autonomic ribbon.
    state_windows = state_windows.sort_values("window_mid_h").copy()
    for _, row in state_windows.iterrows():
        start = float(row["window_start_s"]) / 3600.0
        end = float(row["window_end_s"]) / 3600.0
        draw.rectangle((sx(start), ribbon_top, sx(end), ribbon_bottom), fill=STATE_COLORS.get(row["state"], "#E4E7EC"))
    draw.rectangle((plot_left, ribbon_top, plot_right, ribbon_bottom), outline="#344054", width=2)
    draw_text(draw, (plot_left, ribbon_top - 28), "Autonomic dominance ribbon", 22, "#344054", True)
    legend_x = plot_right - 560
    for label, color in [("sympathetic", STATE_COLORS["sympathetic"]), ("parasympathetic", STATE_COLORS["parasympathetic"]), ("transition", STATE_COLORS["transition"])]:
        draw.rectangle((legend_x, ribbon_top - 32, legend_x + 24, ribbon_top - 8), fill=color)
        draw_text(draw, (legend_x + 32, ribbon_top - 34), label, 18, "#475467")
        legend_x += 185

    # Time axis.
    for h in np.linspace(x_min, x_max, 7):
        x = sx(float(h))
        draw.line((x, ribbon_bottom, x, ribbon_bottom + 12), fill="#667085", width=1)
        draw_text(draw, (x, ribbon_bottom + 18), f"{h:.1f}h", 16, "#667085", anchor="ma")

    # RMSSD plot.
    rm = valid[valid["ln_rmssd"].notna()].copy()
    rm["ln_rmssd_smooth"] = smooth_series(rm["ln_rmssd"], 9)
    rm = rm[rm["ln_rmssd_smooth"].notna()].copy()
    top, bottom = 310, 850
    draw.rectangle((plot_left, top, plot_right, bottom), outline="#344054", width=2)
    draw_text(draw, (plot_left, top - 35), "lnRMSSD trend segmentation", 24, "#344054", True)
    if not rm.empty:
        y_vals = rm["ln_rmssd_smooth"].to_numpy(dtype=float)
        y_min = float(np.nanpercentile(y_vals, 3))
        y_max = float(np.nanpercentile(y_vals, 97))
        if y_max <= y_min:
            y_max = y_min + 1.0
        pad = (y_max - y_min) * 0.15
        y_min -= pad
        y_max += pad

        def sy(value: float) -> float:
            return bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

        for frac in np.linspace(0, 1, 5):
            yy = top + frac * (bottom - top)
            draw.line((plot_left, yy, plot_right, yy), fill="#EAECF0", width=1)
            val = y_max - frac * (y_max - y_min)
            draw_text(draw, (plot_left - 12, yy), f"{val:.2f}", 16, "#667085", anchor="rm")

        points = [(sx(float(t)), sy(float(y))) for t, y in zip(rm["window_mid_h"], rm["ln_rmssd_smooth"])]
        if len(points) > 1:
            draw.line(points, fill="#101828", width=3)

        for _, seg in seg_df.iterrows():
            color = SEGMENT_COLORS.get(seg["trend_type"], "#667085")
            x0 = sx(float(seg["start_h"]))
            x1 = sx(float(seg["end_h"]))
            y0 = sy(float(seg["lnrmssd_fitted_start"]))
            y1 = sy(float(seg["lnrmssd_fitted_end"]))
            draw.line((x0, y0, x1, y1), fill=color, width=7)
            mid_x = (x0 + x1) / 2
            mid_y = (y0 + y1) / 2
            draw_text(draw, (mid_x, mid_y - 16), str(int(seg["segment_id"])), 18, color, True, anchor="mm")

    stats = summarize_for_plot(state_windows, seg_df)
    draw_text(draw, (70, 900), stats, 21, "#475467")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def summarize_for_plot(state_windows: pd.DataFrame, seg_df: pd.DataFrame) -> str:
    valid = state_windows[state_windows["state"] != "invalid"]
    total = max(len(valid), 1)
    sp = (valid["state"].eq("sympathetic").sum() / total) * 100
    pp = (valid["state"].eq("parasympathetic").sum() / total) * 100
    tp = (valid["state"].eq("transition").sum() / total) * 100
    rc = int(seg_df["trend_type"].eq("rising").sum()) if not seg_df.empty else 0
    fc = int(seg_df["trend_type"].eq("falling").sum()) if not seg_df.empty else 0
    sc = int(seg_df["trend_type"].eq("stable").sum()) if not seg_df.empty else 0
    return f"Autonomic: sympathetic {sp:.1f}% | parasympathetic {pp:.1f}% | transition {tp:.1f}%     RMSSD segments: rising {rc}, falling {fc}, stable {sc}"


def process() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    windows = load_target_windows()
    summary_rows = []
    state_episode_rows = []
    segment_rows = []

    for (user, day), group in windows.groupby(["user", "day"], sort=True):
        group = group.sort_values("window_mid_h").copy()
        auto_base = group[group["lf_hf"].notna()].copy()
        auto_base["lf_hf_smooth"] = smooth_series(auto_base["lf_hf"], 9)
        auto_base["state"] = auto_base["lf_hf_smooth"].map(classify_lfhf)
        episodes, auto_summary = state_episodes(group)
        if not episodes.empty:
            episodes.insert(0, "day", day)
            episodes.insert(0, "user", user)
            state_episode_rows.append(episodes)

        rm = group[(group["valid_ratio"] >= 0.80) & group["ln_rmssd"].notna()].sort_values("window_mid_h").copy()
        rm["ln_rmssd_smooth"] = smooth_series(rm["ln_rmssd"], 9)
        rm = rm[rm["ln_rmssd_smooth"].notna()].copy()
        seg_df, rm_summary = piecewise_linear_segments(
            rm["window_mid_h"].to_numpy(dtype=float),
            rm["ln_rmssd_smooth"].to_numpy(dtype=float),
            rm["rmssd_ms"].to_numpy(dtype=float),
        )
        if not seg_df.empty:
            seg_df.insert(0, "day", day)
            seg_df.insert(0, "user", user)
            segment_rows.append(seg_df)

        row = {
            "user": user,
            "day": int(day),
            "night_duration_h": float(group["window_end_s"].max() / 3600.0),
            "all_windows": int(len(group)),
            "valid_rmssd_windows": int(len(rm)),
            "ln_rmssd_median": float(rm["ln_rmssd"].median()) if not rm.empty else np.nan,
            "rmssd_median_ms": float(rm["rmssd_ms"].median()) if not rm.empty else np.nan,
            "lf_hf_median": float(group["lf_hf"].median()),
            "hf_nu_median": float(group["hf_nu"].median()),
            **auto_summary,
            **rm_summary,
        }
        summary_rows.append(row)

        fig_path = OUT_DIR / "figures" / user / f"{user}_day{int(day):02d}_structure.png"
        plot_night(group, auto_base, seg_df, fig_path)

    summary = pd.DataFrame(summary_rows).sort_values(["user", "day"])
    episodes_out = pd.concat(state_episode_rows, ignore_index=True) if state_episode_rows else pd.DataFrame()
    segments_out = pd.concat(segment_rows, ignore_index=True) if segment_rows else pd.DataFrame()
    summary.to_csv(OUT_DIR / "night_structure_summary.csv", index=False, encoding="utf-8-sig")
    episodes_out.to_csv(OUT_DIR / "autonomic_state_episodes.csv", index=False, encoding="utf-8-sig")
    segments_out.to_csv(OUT_DIR / "rmssd_trend_segments.csv", index=False, encoding="utf-8-sig")
    build_user_summary(summary).to_csv(OUT_DIR / "user_structure_summary.csv", index=False, encoding="utf-8-sig")
    return summary, episodes_out, segments_out


def build_user_summary(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for user, g in summary.groupby("user", sort=True):
        rows.append(
            {
                "user": user,
                "nights": int(len(g)),
                "first_day": int(g["day"].min()),
                "last_day": int(g["day"].max()),
                "mean_sympathetic_pct": float(g["sympathetic_pct"].mean()),
                "mean_parasympathetic_pct": float(g["parasympathetic_pct"].mean()),
                "mean_transition_pct": float(g["transition_pct"].mean()),
                "mean_sympathetic_episode_max_min": float(g["sympathetic_episode_max_min"].mean()),
                "mean_parasympathetic_episode_max_min": float(g["parasympathetic_episode_max_min"].mean()),
                "mean_transition_episode_max_min": float(g["transition_episode_max_min"].mean()),
                "mean_rmssd_segment_count": float(g["rmssd_segment_count"].mean()),
                "mean_rising_stage_count": float(g["rising_stage_count"].mean()),
                "mean_falling_stage_count": float(g["falling_stage_count"].mean()),
                "mean_stable_stage_count": float(g["stable_stage_count"].mean()),
                "mean_total_rising_duration_pct": float(g["total_rising_duration_pct"].mean()),
                "mean_total_falling_duration_pct": float(g["total_falling_duration_pct"].mean()),
                "mean_total_stable_duration_pct": float(g["total_stable_duration_pct"].mean()),
                "mean_largest_rise_delta": float(g["largest_rise_delta"].mean()),
                "mean_largest_fall_delta": float(g["largest_fall_delta"].mean()),
                "mean_segment_volatility": float(g["mean_segment_volatility"].mean()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    summary, episodes, segments = process()
    print(f"summary={OUT_DIR / 'night_structure_summary.csv'} rows={len(summary)}")
    print(f"episodes={OUT_DIR / 'autonomic_state_episodes.csv'} rows={len(episodes)}")
    print(f"segments={OUT_DIR / 'rmssd_trend_segments.csv'} rows={len(segments)}")
    print(f"user_summary={OUT_DIR / 'user_structure_summary.csv'}")
    print(summary.groupby("user")["day"].agg(["count", "min", "max"]).to_string())


if __name__ == "__main__":
    main()

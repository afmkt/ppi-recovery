"""Pipeline & data-quality metrics for the NewBoom dashboard.

Deliberately marimo-free: this module is pure data queries + altair chart
specs + text. ``vis.py`` imports it and wraps the results in marimo UI, so the
logic stays importable and testable without a running notebook. Every query
function takes a ``query`` callable (``SQL -> pandas.DataFrame``) so it can be
exercised against any engine.
"""

from __future__ import annotations

import math
from typing import Any

import altair as alt
import pandas as pd
from altair.typing import EncodeKwds

# --- physiological sanity bounds --------------------------------------------
# Device-raw scale. See docs/new-interface.md §3.1: spo2 is stored raw ×10
# (950 = 95.0%); a 0 means "device did not report spo2 this second", not 0%.
HR_MIN, HR_MAX = 30, 220  # bpm
SPO2_MIN, SPO2_MAX = 800, 1000  # raw ×10 -> 80.0% .. 100.0%
PPI_MIN, PPI_MAX = 300, 2000  # ms between two beats
STALE_HOURS = 48.0  # ingestion considered stale past this age
FULL_DAY_S = 86_400  # seconds in a full per-second ingestion day


# ===========================================================================
# Formatting helpers
# ===========================================================================


def fmt_dur(hours) -> str:
    """Render an hour-delta as a compact human duration."""
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(hours):
        return "—"
    if hours < 0:
        return f"-{abs(hours):.1f}h"
    d = int(hours // 24)
    h = int(hours % 24)
    if d:
        return f"{d}d {h}h"
    if hours < 1:
        return f"{int(hours * 60)}m"
    return f"{h}h"


def fmt_ts(ts) -> str:
    """Format a pandas Timestamp / NaT / None as 'YYYY-MM-DD HH:MM'."""
    try:
        if ts is None or (hasattr(ts, "isna") and ts.isna()):
            return "—"
        return ts.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError, TypeError):
        return "—"


def fmt_num(x, digits=0) -> str:
    """Safe numeric render for possibly-null aggregation scalars."""
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return "—"
        v = float(x)
        if digits == 0:
            return f"{round(v):,}"
        return f"{v:,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _chart_df(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows with any missing value before handing a frame to Altair.

    A NULL/NaT cell means the layer/day has no data yet; Altair's JSON export
    (``to_json(allow_nan=False)``) rejects NaN/NaT, so such rows are omitted
    rather than crashing the dashboard.
    """
    return df.dropna()


# ===========================================================================
# Feature 1 — ingestion health & pipeline throughput
# ===========================================================================


def fleet_freshness(query, devices: pd.DataFrame) -> pd.DataFrame:
    """Per-device freshness table: last-PPI age, coverage, task lags."""
    agg = query(
        """
        select device_id,
               min(time)  as first_ppi,
               max(time)  as last_ppi,
               count(*)   as ppi_rows,
               count(distinct date(time)) as active_days,
               sum(case when fresh then 1 else 0 end) as fresh_rows
        from ppi group by device_id
        """
    )
    raw = devices.merge(agg, left_on="id", right_on="device_id", how="left")

    raw["first_ppi"] = pd.to_datetime(raw["first_ppi"], errors="coerce")
    raw["last_ppi"] = pd.to_datetime(raw["last_ppi"], errors="coerce")
    now = pd.Timestamp.now()
    raw["age_h"] = (now - raw["last_ppi"]).dt.total_seconds() / 3600.0
    raw["stale"] = raw["age_h"] > STALE_HOURS
    raw["active_days"] = raw["active_days"].fillna(0).astype(int)
    raw["ppi_rows"] = raw["ppi_rows"].fillna(0).astype(int)
    raw["fresh_rows"] = raw["fresh_rows"].fillna(0).astype(int)
    raw["fresh_pct"] = (
        100.0 * raw["fresh_rows"] / raw["ppi_rows"].replace(0, pd.NA)
    ).fillna(0.0)

    # one end_time column per task, then each task's lag back to "now"
    prog = query("select device_id, task, end_time from progress")
    piv = prog.pivot(index="device_id", columns="task", values="end_time")
    raw = raw.merge(piv, left_on="device_id", right_index=True, how="left")
    for task, col in (
        ("WINDOW_METRIC", "window"),
        ("MINUTE_METRIC", "minute"),
        ("DAILY_SUMMARY", "daily"),
    ):
        raw[col] = (
            pd.NaT
            if task not in piv.columns
            else pd.to_datetime(raw[task], errors="coerce")
        )
        raw[f"lag_{col}_h"] = (now - raw[col]).dt.total_seconds() / 3600.0

    raw["age"] = raw.apply(
        lambda r: "—" if pd.isna(r["last_ppi"]) else fmt_dur(r["age_h"]), axis=1
    )
    raw["status"] = raw["stale"].map({True: "STALE", False: "LIVE"})
    return raw


def fleet_throughput(query) -> pd.DataFrame:
    """Per-device daily PPI row counts (one row per device/day)."""
    return query(
        "select d.mac as device, date(p.time) as day, count(*) as rows "
        "from ppi p join device d on d.id = p.device_id "
        "group by p.device_id, date(p.time)"
    )


def fleet_header(fresh: pd.DataFrame) -> str:
    """One-line fleet health summary for the top of the overview."""
    n_live = int((~fresh["stale"]).sum())
    n_stale = int(fresh["stale"].sum())
    total_ppi = int(fresh["ppi_rows"].sum())
    last_any = pd.to_datetime(fresh["last_ppi"], errors="coerce").max()
    return (
        "### Ingestion health  \n"
        f"**{n_live}** LIVE · **{n_stale}** STALE  ·  "
        f"**{total_ppi:,}** PPI rows  ·  latest PPI **{fmt_ts(last_any)}**"
    )


def throughput_chart(daily: pd.DataFrame):
    """Grouped bar of PPI rows/day; color by device only when the column exists."""
    ref = pd.DataFrame([{"v": FULL_DAY_S}])
    enc: EncodeKwds = {
        "x": alt.X("day:T", title="date", scale=alt.Scale(zero=False)),
        "y": alt.Y("rows:Q", title="PPI rows / day"),
    }
    if "device" in daily.columns:
        enc["color"] = alt.Color("device:N", title="device")
    base = (
        alt.Chart(_chart_df(daily))
        .mark_bar()
        .encode(**enc)
        .properties(width=560, height=260)
    )
    layer = alt.Chart(ref).mark_rule(color="crimson", strokeDash=[4, 2]).encode(y="v:Q")
    return base + layer


def ingestion_meta(query, device_id: int) -> dict:
    """Per-device ingestion summary (last/first time, age, state, counts)."""
    row = query(
        f"""select max(time) last_ppi, min(time) first_ppi, count(*) rows,
                   count(distinct date(time)) as active_days,
                   sum(case when fresh then 1 else 0 end) as fresh_rows
            from ppi where device_id = {int(device_id)}"""
    ).iloc[0]
    last = pd.to_datetime(row["last_ppi"], errors="coerce")
    first = pd.to_datetime(row["first_ppi"], errors="coerce")
    now = pd.Timestamp.now()
    age_h = None if pd.isna(last) else (now - last).total_seconds() / 3600.0
    state = "LIVE" if (age_h is not None and age_h <= STALE_HOURS) else "STALE"
    return {
        "last": last,
        "first": first,
        "age_h": age_h,
        "state": state,
        "rows": int(row["rows"] or 0),
        "active_days": int(row["active_days"] or 0),
        "fresh_rows": int(row["fresh_rows"] or 0),
    }


def ingestion_daily(query, device_id: int) -> pd.DataFrame:
    """Per-day PPI counts with the coverage gap vs a full day."""
    d = query(
        f"select date(time) as day, count(*) as rows "
        f"from ppi where device_id = {int(device_id)} group by date(time) order by day"
    )
    d["gap_pct"] = 100.0 * (FULL_DAY_S - d["rows"]) / FULL_DAY_S
    return d.sort_values("day")


def ingestion_gaps(daily: pd.DataFrame, last) -> pd.DataFrame:
    """Top 5 most-gappy *closed* days (the in-progress partial day excluded)."""
    last_d = pd.to_datetime(last).normalize()
    closed = daily[pd.to_datetime(daily["day"]) < last_d]
    return closed.sort_values("gap_pct", ascending=False).head(5)


def ingestion_summary(meta: dict) -> str:
    return (
        "#### Ingestion throughput  \n"
        f"latest **{fmt_ts(meta['last'])}** · **{fmt_dur(meta['age_h'])}** old · "
        f"**{meta['state']}**  \n"
        f"window **{fmt_ts(meta['first'])}** → **{fmt_ts(meta['last'])}** · "
        f"**{meta['rows']:,}** rows over **{meta['active_days']}** active days · "
        f"fresh **{meta['fresh_rows']:,}**"
    )


# --- raw-input coverage & quality ------------------------------------------
# The two direct measures of the raw PPI feed, shown as the overview headline:
# how *complete* the feed is (received vs an expected per-second stream) and how
# much of what arrived is *unusable* (a raw second the cleaner could not turn
# into a beat interval). Both are device-wide over the whole PPI history, so
# they read as a single headline; the per-day coverage chart below drills into
# the completeness of each calendar day.

def raw_input_quality(query, device_id: int) -> dict:
    """Device-wide raw-PPI coverage and quality over the whole PPI history.

    One aggregate scan of ``ppi`` returns the four raw counts; the ratios are
    derived in Python:

      * ``coverage_pct`` = 100 · total / (active_days · ``FULL_DAY_S``). A full
        per-second stream is 86,400 rows/day, so a device that reports a
        complete day reads 100%. Expected is over the days the device was
        *active*, so a device that stops and resumes is not penalised for the
        gap between.
      * ``invalid_pct``  = 100 · invalid / total. ``invalid`` is the PPI seconds
        the cleaner *scored* unusable — ``ppi_clean`` is NULL among processed
        (not-fresh) rows: a raw value out of physiological range or a local
        outlier the ±5 local-median check rejected (the pipeline's own
        ``valid_ratio`` inverse, ``1 - valid_ratio``).

    Not-yet-scored seconds are ``fresh`` (``ppi_clean`` still NULL until
    ``clean_ppi`` runs), so they are counted as ``pending`` and **excluded from
    invalid** — a device that just ingested a file must not read as 100%
    invalid. ``total`` is all received seconds (the denominator the user asked
    for: "invalid over total data points").
    """
    row = query(
        f"""select
              count(*) as total,
              count(distinct date(time)) as active_days,
              sum(case when ppi_clean is null then 1 else 0 end) as null_clean,
              sum(case when fresh then 1 else 0 end) as pending
            from ppi where device_id = {int(device_id)}"""
    ).iloc[0]
    total = int(row["total"] or 0)
    active_days = int(row["active_days"] or 0)
    # Fresh rows are always un-scored (ppi_clean NULL until clean_ppi runs), so
    # the processed-but-rejected remainder is the genuine "invalid" count.
    pending = int(row["pending"] or 0)
    null_clean = int(row["null_clean"] or 0)
    invalid = max(null_clean - pending, 0)
    valid = total - invalid - pending
    expected = active_days * FULL_DAY_S
    coverage_pct = (100.0 * total / expected) if expected else 0.0
    invalid_pct = (100.0 * invalid / total) if total else 0.0
    return {
        "total": total,
        "active_days": active_days,
        "expected": expected,
        "coverage_pct": coverage_pct,
        "invalid": invalid,
        "valid": valid,
        "pending": pending,
        "invalid_pct": invalid_pct,
    }


def coverage_quality_md(quality: dict) -> str:
    """One-line explainer for the two raw-input ratios in the table below."""
    return (
        "Two direct measures of the raw PPI feed — **coverage** is the seconds "
        "received against an expected per-second stream "
        f"({quality['active_days']} active day(s) × 86,400 s); **invalid** is "
        "the received seconds the cleaner could not turn into a beat interval "
        "(out of physiological range or a local outlier), over all received."
    )


def coverage_quality_table(quality: dict) -> pd.DataFrame:
    """The two raw-input ratios as a 2-row table; empty when the device has no
    PPI data yet (the panel then shows a no-data placeholder)."""
    if not quality["total"]:
        return pd.DataFrame(columns=["measure", "value", "detail"])
    pending_note = (
        f" · {fmt_num(quality['pending'])} not yet scored"
        if quality["pending"]
        else ""
    )
    rows = [
        {
            "measure": "Coverage — received / expected",
            "value": f"{quality['coverage_pct']:.1f}%",
            "detail": (
                f"{fmt_num(quality['total'])} / {fmt_num(quality['expected'])} "
                f"PPI seconds · {quality['active_days']} active day(s)"
            ),
        },
        {
            "measure": "Invalid — of total received",
            "value": f"{quality['invalid_pct']:.1f}%",
            "detail": (
                f"{fmt_num(quality['invalid'])} of {fmt_num(quality['total'])} "
                f"PPI seconds unusable{pending_note}"
            ),
        },
    ]
    return pd.DataFrame(rows, columns=["measure", "value", "detail"])


# ===========================================================================
# Feature 2 — aggregation pipeline monitoring
# ===========================================================================


def aggregation_progress(query, device_id: int) -> pd.DataFrame:
    """Task watermarks with age and lag back to the raw PPI feed."""
    raw_max = pd.to_datetime(
        query(f"select max(time) t from ppi where device_id = {int(device_id)}").iloc[
            0
        ]["t"],
        errors="coerce",
    )
    prog = query(
        f"""select task, end_time from progress where device_id = {int(device_id)}
            order by case task when 'WINDOW_METRIC' then 1
                 when 'MINUTE_METRIC' then 2 else 3 end"""
    )
    prog["end_time"] = pd.to_datetime(prog["end_time"], errors="coerce")
    now = pd.Timestamp.now()
    prog["age_h"] = (now - prog["end_time"]).dt.total_seconds() / 3600.0
    prog["lag_raw_h"] = (raw_max - prog["end_time"]).dt.total_seconds() / 3600.0
    prog["age"] = prog["age_h"].apply(fmt_dur)
    prog["lag_raw"] = prog["lag_raw_h"].apply(fmt_dur)
    return prog


def aggregation_production(query, device_id: int) -> pd.DataFrame:
    """Per-day production counts of the two downstream aggregations."""
    wm = query(
        f"select date(window_start) as day, count(*) as n "
        f"from windowmetric where device_id = {int(device_id)} group by date(window_start)"
    )
    mm = query(
        f"select date(timestamp) as day, count(*) as n "
        f"from minutemetric where device_id = {int(device_id)} group by date(timestamp)"
    )
    prod = wm.merge(mm, on="day", how="outer", suffixes=("_wm", "_mm"))
    return prod[["day", "n_wm", "n_mm"]].sort_values("day")


def production_chart(prod: pd.DataFrame):
    """Grouped bar of windowmetric vs minutemetric rows/day.

    The two series are relabeled to human names so the legend and tooltip read
    ``Window metric`` / ``Minute metric`` instead of the raw columns ``n_wm`` /
    ``n_mm``.
    """
    melt = (
        prod.rename(columns={"n_wm": "Window metric", "n_mm": "Minute metric"})
        .melt(id_vars="day", var_name="metric", value_name="n")
    )
    return (
        alt.Chart(_chart_df(melt))
        .mark_bar()
        .encode(
            x=alt.X("day:T", title="date", scale=alt.Scale(zero=False)),
            y=alt.Y("n:Q", title="rows / day"),
            color=alt.Color("metric:N", title=""),
        )
        .properties(width=560, height=240, title="Downstream production — rows / day")
    )


def aggregation_summary(raw_max) -> str:
    return (
        "#### Aggregation watermarks  \n"
        f"raw PPI reaches **{fmt_ts(raw_max)}**. Each task's `end_time` is the "
        f"last instant it has processed; **lag_raw** is the gap back to the raw "
        f"feed (0 = caught up)."
    )


# ===========================================================================
# Feature 3 — data quality & statistical sanity
# ===========================================================================


def quality_metrics(query, device_id: int) -> pd.Series:
    """One scan of ppi: ranges + out-of-band counts + total."""
    return query(
        f"""select
              min(hr) hr_min, max(hr) hr_max, round(avg(hr),1) hr_mean,
              min(spo2) spo2_min, max(spo2) spo2_max, round(avg(spo2),1) spo2_mean,
              min(ppi) ppi_min, max(ppi) ppi_max, round(avg(ppi),1) ppi_mean,
              sum(case when hr < {HR_MIN} or hr > {HR_MAX} then 1 else 0 end) hr_oob,
              sum(case when spo2 = 0 then 1 else 0 end) spo2_zero,
              sum(case when spo2 > {SPO2_MAX} or (spo2 < {SPO2_MIN} and spo2 <> 0) then 1 else 0 end) spo2_oob,
              sum(case when ppi < {PPI_MIN} or ppi > {PPI_MAX} then 1 else 0 end) ppi_oob,
              count(*) n
            from ppi where device_id = {int(device_id)}"""
    ).iloc[0]


def quality_checks(s: pd.Series) -> pd.DataFrame:
    """Turn raw ppi aggregates into a PASS/WARN/FAIL sanity table."""
    n = int(s["n"] or 0)
    checks = []

    def add(metric, oob, note):
        oob = int(oob or 0)
        pct = 100.0 * oob / n if n else 0.0
        verdict = "PASS" if oob == 0 else ("WARN" if pct < 5 else "FAIL")
        checks.append([metric, f"{oob:,} ({pct:.1f}%)", verdict, note])

    add(
        "HR out of band",
        s["hr_oob"],
        f"outside {HR_MIN}–{HR_MAX} bpm (min {fmt_num(s['hr_min'])}, max {fmt_num(s['hr_max'])})",
    )
    add("SpO₂ not reported", s["spo2_zero"], "spo2 = 0 (no report this second)")
    add(
        "SpO₂ out of band",
        s["spo2_oob"],
        f"outside {SPO2_MIN}–{SPO2_MAX} raw (80–100%)",
    )
    add(
        "PPI out of band",
        s["ppi_oob"],
        f"outside {PPI_MIN}–{PPI_MAX} ms (min {fmt_num(s['ppi_min'])}, max {fmt_num(s['ppi_max'])})",
    )
    return pd.DataFrame(checks, columns=["check", "count", "verdict", "note"])


def quality_ranges_md(s: pd.Series) -> str:
    mean = s["spo2_mean"]
    spo2_pct = "—" if pd.isna(mean) else f"{float(mean) / 10:.1f}%"
    return (
        f"#### Value ranges (n = {int(s['n'] or 0):,} per-second rows)  \n"
        f"HR **{fmt_num(s['hr_min'])}–{fmt_num(s['hr_max'])}** (μ {fmt_num(s['hr_mean'], 1)}) · "
        f"SpO₂ **{fmt_num(s['spo2_min'])}–{fmt_num(s['spo2_max'])}** raw "
        f"(μ {fmt_num(s['spo2_mean'], 1)} = {spo2_pct}) · "
        f"PPI **{fmt_num(s['ppi_min'])}–{fmt_num(s['ppi_max'])} ms** (μ {fmt_num(s['ppi_mean'], 1)})"
    )


def window_summary(query, device_id: int) -> dict:
    """windowmetric state distribution + valid_ratio quality."""
    states = query(
        f"select state, count(*) as windows "
        f"from windowmetric where device_id = {int(device_id)} group by state order by windows desc"
    )
    wq = query(
        f"""select round(min(valid_ratio),3) vmin, round(max(valid_ratio),3) vmax,
                   round(avg(valid_ratio),3) vmean,
                   sum(case when valid_ratio < 0.5 then 1 else 0 end) low, count(*) n
            from windowmetric where device_id = {int(device_id)}"""
    ).iloc[0]
    total = int(states["windows"].sum()) if not states.empty else 0
    if total:
        states["pct"] = (100.0 * states["windows"] / total).round(1)
    return {"states": states, "quality": wq, "total": total}


def state_chart(states: pd.DataFrame):
    """Horizontal bar of window counts by autonomic state."""
    return (
        alt.Chart(_chart_df(states))
        .mark_bar()
        .encode(
            x=alt.X("windows:Q", title="windows"),
            y=alt.Y("state:N", title="state"),
            color=alt.Color("state:N", title="state"),
        )
        .properties(width=360, height=max(120, 26 * max(1, len(states))))
    )


def window_quality_md(wq: pd.Series, total: int) -> str:
    return (
        "#### Window-metric quality  \n"
        f"**{total:,}** windows · valid_ratio μ **{fmt_num(wq['vmean'], 3)}** "
        f"({fmt_num(wq['vmin'], 3)}–{fmt_num(wq['vmax'], 3)}) · "
        f"**{fmt_num(wq['low'])}** windows below 0.5 valid"
    )


def daily_summary(query, device_id: int, limit: int = 14) -> pd.DataFrame:
    """Recent daily summaries: baselines vs raw + status score."""
    return query(
        f"""select summary_date, status_score, base_score, sleep_score,
                   is_temporary_algorithm as tmp,
                   rest_heart_rate_val as rhr, hrv_night_val as hrv
            from dailysummary where device_id = {int(device_id)}
            order by summary_date desc limit {int(limit)}"""
    )


# Pipeline layer order (raw -> derived) and human labels. The DB stores the layer
# enum name (e.g. RAW_PPI), so ordering/labels are matched case-insensitively.
_LAYER_ORDER = [
    "raw_ppi", "cleaned_ppi", "window_metric",
    "minute_metric", "hourly_status", "daily_summary",
]
LAYER_LABELS = {
    "raw_ppi": "Raw PPI",
    "cleaned_ppi": "Cleaned PPI",
    "window_metric": "Window",
    "minute_metric": "Minute",
    "hourly_status": "Hourly",
    "daily_summary": "Daily",
}


def layer_freshness(query, device_id: int, now=None) -> pd.DataFrame:
    """Six pipeline layers (raw -> derived): newest data time, row count, lag.

    ``lag`` is *live* when ``now`` is given (``now - latest_data_time`` — a frozen
    device reads stale, which is the point of a status view); when omitted it
    falls back to the worker's stored ``lag_s`` (as of the last diagnostic scan).
    NULL data times render as ``—`` (no data yet), never NaN. A ``stage`` column
    holds the human label for each layer.
    """
    df = query(
        f"""
        select layer, latest_data_time, row_count, lag_s, last_scan_at
        from layerfreshness
        where device_id = {int(device_id)}
        """
    )
    df["latest_data_time"] = pd.to_datetime(df["latest_data_time"], errors="coerce")
    if now is None:
        df["lag_h"] = df["lag_s"] / 3600.0
    else:
        # Live lag. Both 'now' and the stored latest are naive local, so a direct
        # subtraction is safe.
        df["lag_h"] = (pd.Timestamp(now) - df["latest_data_time"]).dt.total_seconds() / 3600.0
    df["lag"] = df["lag_h"].apply(fmt_dur)
    df["stage"] = df["layer"].str.lower().map(LAYER_LABELS).fillna(df["layer"])
    df["_order"] = df["layer"].str.lower().map(
        {name: i for i, name in enumerate(_LAYER_ORDER)}
    )
    df = df.sort_values("_order", kind="stable").drop(columns="_order")
    return df


def layer_lag_chart(df: pd.DataFrame):
    """Horizontal bar of lag (hours) per pipeline layer."""
    return (
        alt.Chart(_chart_df(df))
        .mark_bar()
        .encode(
            x=alt.X("lag_h:Q", title="lag (hours)"),
            y=alt.Y("layer:N", title="layer", sort=None),
            color=alt.condition(
                alt.datum.lag_h > 1,  # threshold you care about
                alt.value("crimson"),
                alt.value("steelblue"),
            ),
            tooltip=["layer", "lag", "row_count", "latest_data_time"],
        )
        .properties(width=480, height=220, title="Pipeline layer lag")
    )


def coverage_chart(daily: pd.DataFrame, full_day_s: float = FULL_DAY_S) -> alt.Chart:
    """Per-day raw-PPI coverage: one stacked bar per calendar day.

    Each day is a full-height (100%) bar split into a *Covered* segment (the PPI
    rows actually ingested that day) and a *Gap* segment (the rest of a full
    day), so a 0%-coverage day reads as a solid gap bar instead of an absent
    one. Days with no PPI at all are filled in from the observed day range as
    100%-gap bars, so a broken feed shows up as a visible gap in the row.
    """
    d = daily[["day", "rows"]].copy()
    if d.empty:
        return alt.Chart(pd.DataFrame(columns=["day"])).mark_bar().properties(
            width=560, height=240, title="Daily PPI coverage — no data"
        )
    d["day"] = pd.to_datetime(d["day"]).dt.normalize()
    # Fill the whole observed range so missing days become full-gap bars.
    rng = pd.date_range(d["day"].min(), d["day"].max(), freq="D")
    d = d.set_index("day").reindex(rng, fill_value=0).reset_index()
    d.columns = ["day", "rows"]
    d["rows"] = d["rows"].clip(upper=full_day_s)
    d["covered_pct"] = 100.0 * d["rows"] / full_day_s
    d["gap_pct"] = 100.0 - d["covered_pct"]
    long = d.melt(
        id_vars="day", var_name="part", value_name="pct",
        value_vars=["covered_pct", "gap_pct"],
    )
    long["part"] = long["part"].map({"covered_pct": "Covered", "gap_pct": "Gap"})
    return (
        alt.Chart(_chart_df(long))
        .mark_bar()
        .encode(
            x=alt.X("day:T", title="date", scale=alt.Scale(zero=False)),
            y=alt.Y(
                "pct:Q", title="coverage %", stack=True, scale=alt.Scale(domain=[0, 100])
            ),
            color=alt.Color(
                "part:N",
                title="",
                scale=alt.Scale(domain=["Covered", "Gap"], range=["#2ca02c", "#d62728"]),
            ),
            tooltip=["day", "part", alt.Tooltip("pct:Q", title="pct", format=".1f")],
        )
        .properties(width=560, height=240, title="Daily PPI coverage — covered vs gap")
    )


# ===========================================================================
# Feature 4 — raw/derived table browser
#
# The per-second PPI table is a million rows; per-minute/per-window tables are
# ~20k. None of them can be shipped to the browser whole, so every builder is
# bounded server-side: a time-window ``cutoff`` plus a hard ``cap`` (a
# ``LIMIT``). The bounded slice is then handed to a marimo table for client-side
# paging/search. All four time columns are indexed, so the bounding query is
# fast. Each builder takes the same ``query`` callable (``SQL -> DataFrame``)
# as the rest of the module, so it is testable against any engine.
# ===========================================================================

# Per-second window presets for the time-series tables. ``None`` = no lower
# bound ("All"). Values are in seconds.
_WINDOW_PRESETS = {
    "12h": 12 * 3600.0,
    "1h": 3600.0,
    "6h": 6 * 3600.0,
    "24h": 24 * 3600.0,
    "48h": 48 * 3600.0,
    "7d": 7 * 86400.0,
    "30d": 30 * 86400.0,
    "All": None,
}


def window_cutoff(preset, anchor=None):
    """Resolve a window preset to a lower bound relative to ``anchor``, or
    None for "All".

    ``anchor`` is the device's most recent data time (see ``last_data_time``).
    Anchoring to the data rather than wall-clock "now" keeps a preset like
    "1h" meaningful for a device whose latest sample is days old: it means the
    last hour of *data the device has*, so a historical device shows its most
    recent hour instead of an empty future window. A caller that omits
    ``anchor`` falls back to naive-local "now" (a live device).
    """
    delta_s = _WINDOW_PRESETS.get(preset, 3600.0)
    if delta_s is None:
        return None
    if anchor is None:
        anchor = pd.Timestamp.now()
    return pd.Timestamp(anchor) - pd.Timedelta(seconds=float(delta_s))


def last_data_time(query, device_id, table, time_col):
    """Most recent data time for a device's layer, or None when it has none.

    Used to anchor a time window to the device's own data (rather than
    wall-clock "now") so a historical device's "1h" window is its last hour of
    data, not an empty future window. ``table``/``time_col`` are internal, not
    user input, so they are interpolated directly.
    """
    row = query(f"select max({time_col}) as t from {table} where device_id = {int(device_id)}")
    if row.empty:
        return None
    ts = pd.to_datetime(row.iloc[0]["t"], errors="coerce")
    if pd.isna(ts):
        return None
    return ts


def last_lfhf_time(query, device_id):
    """Most recent ``window_start`` that carries an ``lf_hf`` value for a device,
    or None.

    Used to explain why an LF/HF ribbon can be empty even when the window is
    full of windows: HRV (``lf_hf``) is computed only for *valid* windows, so a
    recent all-invalid gap leaves the chart with nothing to plot. ``None`` means
    the device has no LF/HF windows at all.
    """
    row = query(
        "select max(case when lf_hf is not null then window_start end) as t "
        "from windowmetric where device_id = " + str(int(device_id))
    )
    if row.empty:
        return None
    t = row.iloc[0]["t"]
    return None if pd.isna(t) else pd.to_datetime(t, errors="coerce")


def _cutoff_literal(cutoff) -> str:
    """Fixed-width, single-quoted text literal so it compares correctly against
    the stored ``'YYYY-MM-DD HH:MM:SS.ffffff'`` datetime text (a shorter,
    non-padded string would sort wrong lexicographically; an unquoted one is a
    syntax error). The value is a fixed-format timestamp, so no escaping is
    needed."""
    return "'" + pd.Timestamp(cutoff).strftime("%Y-%m-%d %H:%M:%S.%f") + "'"


def _day_literal(day) -> str:
    """A fixed ``YYYY-MM-DD`` value for a calendar day, matching the
    ``date(<col>)`` projection of the stored datetime text. The builder wraps
    it in single quotes (as ``_cutoff_literal`` does), so this returns the bare
    value. Accepts a ``date``/``Timestamp``/``'YYYY-MM-DD'`` string."""
    return pd.Timestamp(day).strftime("%Y-%m-%d")


def day_bounds(day, preset="All"):
    """Resolve the global day picker to a ``(lower_bound, upper_bound)``.

    ``day`` is a ``date``/``Timestamp``/``'YYYY-MM-DD'``. The window preset
    (see ``_WINDOW_PRESETS``) is centered on the middle of that day (noon), so
    a preset like ``12h`` covers the day's middle 12 hours. ``All`` spans the
    whole calendar day (midnight to midnight). A preset wider than a day is fine
    — the per-day ``date(<col>)`` filter in the builders keeps the result inside
    the chosen day. ``None`` means no bound on that side.
    """
    if day is None:
        return None, None
    day_ts = pd.Timestamp(day).normalize()
    preset = _WINDOW_PRESETS.get(preset, 3600.0)
    if preset is None:
        return day_ts, day_ts + pd.Timedelta(days=1)
    half = preset / 2.0
    noon = day_ts + pd.Timedelta(hours=12)
    return noon - pd.Timedelta(seconds=half), noon + pd.Timedelta(seconds=half)


def _day_from_picker(day_picker, query, device_id, table, time_col):
    """Coerce the global day picker to a ``date`` for the builders.

    The picker's ``value`` is a ``'YYYY-MM-DD'`` string (or ``None`` before a
    pick); when it is missing we fall back to the device's own most-recent data
    day, so a freshly loaded device still shows its latest day without a click.
    """
    val = getattr(day_picker, "value", None)
    if val:
        return pd.Timestamp(val).date()
    last = last_data_time(query, device_id, table, time_col)
    return pd.Timestamp.now().date() if last is None else last.date()


def _where_device(device_id, *clauses: str) -> str:
    base = "device_id = " + str(int(device_id))
    return base + (" and " + " and ".join(clauses) if clauses else "")


def cleaned_ppi(query, device_id, cutoff=None, cap=2000,
                cleaned_only=False, fresh_only=False, day=None) -> pd.DataFrame:
    """Most-recent per-second PPI rows for a device, newest first, capped.

    ``spo2`` is stored raw×10 (0 = "not reported this second"); a ``spo2_pct``
    column (÷10, blank when 0) is appended for display. Rows are bounded by
    ``cutoff`` and ``cap`` so the browser never receives the full per-second
    table. When ``day`` is given, rows are restricted to that calendar day.
    """
    clauses: list[str] = []
    if cutoff is not None:
        clauses.append("time > " + _cutoff_literal(cutoff))
    if day is not None:
        clauses.append("date(time) = '" + _day_literal(day) + "'")
    if cleaned_only:
        clauses.append("ppi_clean IS NOT NULL")
    if fresh_only:
        clauses.append("fresh = 1")
    sql = (
        "select time, hr, spo2, ppi, ppi_clean, activity, fresh "
        "from ppi where " + _where_device(device_id, *clauses)
        + f" order by time desc limit {int(cap)}"
    )
    df = query(sql)
    if df.empty:
        return df
    df = df.copy()
    reported = df["spo2"].fillna(0).astype(int) != 0
    df["spo2_pct"] = (df["spo2"] / 10.0).where(reported, other=float("nan"))
    return df[["time", "hr", "spo2_pct", "ppi", "ppi_clean", "activity", "fresh"]]


def window_table(query, device_id, cutoff=None, cap=5000,
                 states=None, sleep_only=False, min_valid=0.0, day=None) -> pd.DataFrame:
    """Most-recent windowmetrics for a device, newest first, capped and
    optionally filtered by state / sleep / minimum ``valid_ratio``.

    ``states`` matches case-insensitively (the DB stores the enum name, whose
    case is not relied on). When ``day`` is given, windows are restricted to
    that calendar day.
    """
    clauses: list[str] = []
    if cutoff is not None:
        clauses.append("window_start > " + _cutoff_literal(cutoff))
    if day is not None:
        clauses.append("date(window_start) = '" + _day_literal(day) + "'")
    if states:
        ph = ", ".join(f"'{str(s)}'" for s in states)
        clauses.append(f"lower(state) in ({ph})")
    if sleep_only:
        clauses.append("is_sleep = 1")
    clauses.append(f"valid_ratio >= {float(min_valid):.6f}")
    sql = (
        "select night_date, window_start, window_mid_h, state, is_sleep, "
        "valid_ratio, rmssd_ms, ln_rmssd, lf_hf, lf, hf "
        "from windowmetric where " + _where_device(device_id, *clauses)
        + f" order by window_start desc limit {int(cap)}"
    )
    return query(sql)


def minute_table(query, device_id, cutoff=None, cap=5000, day=None) -> pd.DataFrame:
    """Most-recent minutemetrics for a device, newest first, capped.

    When ``day`` is given, minutes are restricted to that calendar day.
    """
    clauses: list[str] = []
    if cutoff is not None:
        clauses.append("timestamp > " + _cutoff_literal(cutoff))
    if day is not None:
        clauses.append("date(timestamp) = '" + _day_literal(day) + "'")
    sql = (
        "select timestamp, night_date, heart_rate, s1_load, tss_load, "
        "total_load, heart_rate_zone, steps, calories "
        "from minutemetric where " + _where_device(device_id, *clauses)
        + f" order by timestamp desc limit {int(cap)}"
    )
    return query(sql)


def hr_load_energy_table(query, device_id, cutoff=None, cap=50000, day=None) -> pd.DataFrame:
    """Most-recent per-minute heart rate, cumulative intraday load, and energy
    reserve for a device, newest first, capped.

    Backs the 48h reference figure (心率 / 日内累计负荷 / 储能):
    ``heart_rate`` is the per-minute mean, ``total_load`` is the cumulative
    intraday load (S1 + tss, reset each night session), and ``total_energy`` is
    the 0-100 body-energy reserve. All three live in ``minutemetric``, so one
    bounded query feeds the whole figure. Per-minute data is small, so the cap
    is generous (``50000`` ≈ 34 days). When ``day`` is given, minutes are
    restricted to that calendar day.
    """
    clauses: list[str] = []
    if cutoff is not None:
        clauses.append("timestamp > " + _cutoff_literal(cutoff))
    if day is not None:
        clauses.append("date(timestamp) = '" + _day_literal(day) + "'")
    sql = (
        "select timestamp, night_date, heart_rate, total_load, total_energy "
        "from minutemetric where " + _where_device(device_id, *clauses)
        + f" order by timestamp desc limit {int(cap)}"
    )
    return query(sql)


def daily_table(query, device_id, day=None) -> pd.DataFrame:
    """Daily-summary rows for a device, newest first (one per day).

    A curated column subset — scores, HRV baselines/vals, energy, autonomic
    balance and the coaching text — rather than all 48 columns. When ``day`` is
    given, only that calendar day's summary is returned.
    """
    clauses = ["summary_date = '" + _day_literal(day) + "'"] if day is not None else []
    return query(
        "select summary_date, status_score, base_score, sleep_score, "
        "rest_heart_rate_val, hrv_night_val, hrv_morning_val, "
        "spo2_night_avg, spo2_night_min, "
        "energy_start_e, energy_end_f, total_load, total_tss, "
        "atl, ctl, tsb, "
        "sympathetic_pct, parasympathetic_pct, transition_pct, "
        "boom_go_text, training_suggestion, is_temporary_algorithm "
        "from dailysummary where " + _where_device(device_id, *clauses)
        + " order by summary_date desc"
    )


# --- Feature 4 chart helpers -----------------------------------------------
# Each returns an Altair spec (NaN-dropped, so ``to_json(allow_nan=False)`` is
# safe) plus a no-data placeholder of the same dimensions when the slice is
# empty, so the panel never collapses.


def _empty(line=True, title="no rows") -> alt.Chart:
    cls = "time:T" if line else "x:N"
    return (
        alt.Chart(pd.DataFrame(columns=[cls]))
        .mark_line()
        .properties(width=560, height=180, title=title)
    )


def ppi_clean_chart(df: pd.DataFrame) -> alt.Chart:
    """Cleaned PPI: ``ppi_clean`` (ms between beats) over time, raw ``ppi``
    faint. Both series share the ms axis; a null in one does not blank the
    other (each is drawn from its own non-null subset)."""
    if df is None or df.empty:
        return _empty(line=True, title="Cleaned PPI — no rows")
    d = df.copy()
    d["time"] = pd.to_datetime(d["time"], errors="coerce")
    d = d.dropna(subset=["time"])
    series = [c for c in ("ppi", "ppi_clean") if c in d.columns and d[c].notna().any()]
    if not series:
        return _empty(line=True, title="Cleaned PPI — no rows")
    m = d[["time"] + series].melt(
        id_vars="time", var_name="series", value_name="ms", value_vars=series
    )
    m["series"] = m["series"].map({"ppi": "raw ppi", "ppi_clean": "ppi_clean"})
    m = m.dropna()
    return (
        alt.Chart(m)
        .mark_line(point=False)
        .encode(
            x=alt.X("time:T", title="time (local)"),
            y=alt.Y("ms:Q", title="ms between beats"),
            color=alt.Color("series:N", title=""),
        )
        .properties(width=560, height=180, title="Cleaned PPI — ms between beats")
    )


def window_ribbon_chart(df: pd.DataFrame) -> alt.Chart:
    """Autonomic ribbon: LF/HF vs hour-from-night-start, colored by state."""
    if df is None or df.empty:
        return _empty(line=True, title="Window ribbon — no LF/HF rows")
    d = df.copy()
    d["window_mid_h"] = pd.to_numeric(d.get("window_mid_h"), errors="coerce")
    d = d.dropna(subset=["window_mid_h"])
    if "lf_hf" not in d.columns or not d["lf_hf"].notna().any():
        return _empty(line=True, title="Window ribbon — no LF/HF rows")
    d = d[d["lf_hf"].notna()]
    # The color scale below fixes a lowercase domain (the WindowState enum
    # *values*), but the DB stores ``state`` as the enum *name* (uppercase —
    # SQLModel's Enum column persists the name, and the dashboard reads the raw
    # VARCHAR via pandas, bypassing the enum). Lowercase it so every plotted
    # state maps to a color; otherwise no point gets a stroke and the ribbon
    # renders empty even though the window is full of LF/HF windows.
    if "state" in d.columns:
        d["state"] = d["state"].astype("string").str.lower()
    return (
        alt.Chart(d)
        .mark_line(point=False)
        .encode(
            x=alt.X("window_mid_h:Q", title="hour from night start"),
            y=alt.Y("lf_hf:Q", title="LF/HF"),
            color=alt.Color(
                "state:N",
                title="state",
                scale=alt.Scale(
                    domain=["sympathetic", "transition", "parasympathetic"],
                    range=["#d62728", "#7f7f7f", "#2ca02c"],
                ),
            ),
        )
        .properties(width=560, height=180, title="Window ribbon — LF/HF by state")
    )


def window_ribbon_has_data(df: pd.DataFrame) -> bool:
    """Whether the window holds any plottable LF/HF point."""
    if df is None or df.empty or "lf_hf" not in df.columns:
        return False
    return bool(df["lf_hf"].notna().any())


def window_ribbon_caption(df: pd.DataFrame, cutoff, anchor, last_lfhf) -> str:
    """A self-explaining header for the LF/HF ribbon.

    Always states the time window and how many windows it holds (and how many
    carry an ``lf_hf``). When none carry one, it also says *why* the chart is
    unavailable — HRV is computed only for valid windows — and when the most
    recent LF/HF window was (or that the device has none at all), so an empty
    chart explains itself instead of failing silently.
    """
    n = int(len(df)) if df is not None else 0
    k = int(df["lf_hf"].notna().sum()) if window_ribbon_has_data(df) else 0
    start = "all-time" if cutoff is None else fmt_ts(cutoff)
    end = fmt_ts(anchor) if anchor is not None else "now"
    head = (
        "### Window ribbon — LF/HF by state  \n"
        f"window **{start}–{end}** · **{n:,}** windows · **{k:,}** with LF/HF"
    )
    if k > 0:
        return head
    if last_lfhf is None:
        why = (
            "no LF/HF in this window — this device has **no LF/HF windows at all**; "
            "HRV (LF/HF) is computed only for valid windows"
        )
    else:
        gap = None
        if cutoff is not None and last_lfhf < cutoff:
            gap = fmt_dur((cutoff - last_lfhf).total_seconds() / 3600.0)
        gap_txt = f", **{gap}** before this window" if gap else ""
        why = (
            f"no LF/HF in this window — the most recent LF/HF window was "
            f"**{fmt_ts(last_lfhf)}**{gap_txt}; HRV is computed only for valid windows"
        )
    return head + "  \n" + why


def minute_hr_chart(df: pd.DataFrame) -> alt.Chart:
    """Per-minute heart rate over time, colored by HR zone."""
    if df is None or df.empty:
        return _empty(line=True, title="Per-minute heart rate — no rows")
    d = df.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    d = d.dropna(subset=["timestamp"])
    if not d["heart_rate"].notna().any():
        return _empty(line=True, title="Per-minute heart rate — no rows")
    d = d[d["heart_rate"].notna()]
    return (
        alt.Chart(d)
        .mark_line(point=False)
        .encode(
            x=alt.X("timestamp:T", title="time (local)"),
            y=alt.Y("heart_rate:Q", title="HR (bpm)"),
            color=alt.Color("heart_rate_zone:N", title="HR zone"),
        )
        .properties(width=560, height=180, title="Per-minute heart rate")
    )


def daily_autonomic_chart(df: pd.DataFrame) -> alt.Chart:
    """Stacked bar of night-window state balance (% sympathetic/para/transition)
    per summary date."""
    if df is None or df.empty:
        return _empty(line=False, title="Autonomic balance — no rows")
    d = df.copy()
    d["summary_date"] = pd.to_datetime(d["summary_date"], errors="coerce")
    d = d.dropna(subset=["summary_date"])
    cols = [c for c in ("sympathetic_pct", "parasympathetic_pct", "transition_pct")
            if c in d.columns]
    if not cols:
        return _empty(line=False, title="Autonomic balance — no rows")
    m = d.melt(
        id_vars="summary_date", var_name="state", value_name="pct", value_vars=cols
    )
    m["state"] = m["state"].str.replace("_pct", "", regex=False)
    m = m.dropna()
    if m.empty:
        return _empty(line=False, title="Autonomic balance — no rows")
    return (
        alt.Chart(m)
        .mark_bar()
        .encode(
            x=alt.X("summary_date:T", title="date"),
            y=alt.Y(
                "pct:Q",
                title="% of night windows",
                stack=True,
                scale=alt.Scale(domain=[0, 100]),
            ),
            color=alt.Color(
                "state:N",
                title="state",
                scale=alt.Scale(
                    domain=["sympathetic", "parasympathetic", "transition"],
                    range=["#d62728", "#2ca02c", "#7f7f7f"],
                ),
            ),
            tooltip=["summary_date", "state", "pct"],
        )
        .properties(
            width=560, height=180,
            title="Autonomic balance — % of night windows by state",
        )
    )


def hr_load_energy_chart(
    df: pd.DataFrame,
) -> alt.Chart | alt.LayerChart | alt.VConcatChart:
    """Heart rate · intraday cumulative load · energy reserve, as one figure with
    three stacked panels that share the time axis (independent y-axes each).

    Mirrors the 48h reference view (心率 / 日内累计负荷 / 储能):
      * **Heart rate** — per-minute ``heart_rate`` plus a 5-minute smoothed
        series (the reference's 分钟均值 / 5分钟平滑).
      * **Load** — cumulative intraday ``total_load`` (S1 + tss). One line per
        night session so the daily reset reads as a sawtooth break, not a drop:
        the data already resets each night, and a single continuous line would
        draw a false plunge from end-of-day to next-day start. Per-session
        grouping means no NaN is injected, keeping ``to_json(allow_nan=False)``
        safe (the contract the dashboard's JSON-render test pins).
      * **Energy** — ``total_energy`` (0-100); continuous, it recovers overnight.
    """
    if df is None or df.empty:
        return _empty(line=True, title="HR · load · energy — no rows")
    d = df.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    # ``night_date`` drives the per-session load break; the query always selects
    # it, but guard for a partial frame (e.g. an energy-only slice).
    if "night_date" in d.columns:
        d["night_date"] = pd.to_datetime(d["night_date"], errors="coerce")
    d = d.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    has_hr = "heart_rate" in d.columns and bool(d["heart_rate"].notna().any())
    has_load = "total_load" in d.columns and bool(d["total_load"].notna().any())
    has_energy = "total_energy" in d.columns and bool(d["total_energy"].notna().any())
    if not (has_hr or has_load or has_energy):
        return _empty(line=True, title="HR · load · energy — no rows")

    # A shared time axis (Vega shares x across a vconcat); independent y-axes
    # because the three series have incommensurable units (bpm / load / 0-100).
    x = alt.X("timestamp:T", title="time", axis=alt.Axis(format="%m-%d %H:%M"))
    panels: list[Any] = []

    if has_hr:
        hr = d[["timestamp", "heart_rate"]].copy()
        hr["heart_rate"] = pd.to_numeric(hr["heart_rate"], errors="coerce")
        hr["hr_5min"] = hr["heart_rate"].rolling(5, min_periods=1).mean()
        long = hr.melt(
            id_vars="timestamp", var_name="series", value_name="bpm",
            value_vars=["heart_rate", "hr_5min"],
        )
        _series_labels = {"heart_rate": "1-min avg", "hr_5min": "5-min smoothed"}
        long["series"] = long["series"].map(_series_labels)
        panels.append(
            alt.Chart(_chart_df(long))
            .mark_line(point=False)
            .encode(
                x=x,
                y=alt.Y("bpm:Q", title="heart rate (bpm)"),
                color=alt.Color(
                    "series:N", title="",
                    scale=alt.Scale(
                        domain=["1-min avg", "5-min smoothed"],
                        range=["#1f77b4", "#ff7f0e"],
                    ),
                ),
                tooltip=[
                    alt.Tooltip("series:N"),
                    alt.Tooltip("bpm:Q", title="bpm", format=".0f"),
                ],
            )
            .properties(height=150, title="Heart rate")
        )

    if has_load:
        load_cols = (
            ["timestamp", "total_load", "night_date"]
            if "night_date" in d.columns
            else ["timestamp", "total_load"]
        )
        load = d[load_cols].copy()
        load["total_load"] = pd.to_numeric(load["total_load"], errors="coerce")
        load = load.dropna(subset=["timestamp", "total_load"])
        # One line per night session so the daily reset reads as a sawtooth break.
        # With no ``night_date`` there is a single unbroken session.
        groups = (
            [g for _, g in load.groupby(load["night_date"], sort=True)]
            if "night_date" in load.columns
            else [load]
        )
        day_layers = [
            g[["timestamp", "total_load"]].dropna(subset=["total_load"]) for g in groups
        ]
        day_layers = [g for g in day_layers if not g.empty]
        if day_layers:
            layers = [
                alt.Chart(_chart_df(g))
                .mark_line(point=False, color="#d95f02")
                .encode(
                    x=x,
                    y=alt.Y("total_load:Q", title="intraday load"),
                    tooltip=[alt.Tooltip("total_load:Q", title="load", format=".3f")],
                )
                for g in day_layers
            ]
            panels.append(
                alt.layer(*layers).properties(
                    height=150, title="Intraday cumulative load (resets daily)"
                )
            )

    if has_energy:
        en = d[["timestamp", "total_energy"]].copy()
        en["total_energy"] = pd.to_numeric(en["total_energy"], errors="coerce")
        en = en.dropna(subset=["total_energy"])
        panels.append(
            alt.Chart(_chart_df(en))
            .mark_line(point=False, color="#2ca02c")
            .encode(
                x=x,
                y=alt.Y(
                    "total_energy:Q",
                    title="energy (0-100)",
                    scale=alt.Scale(domain=[0, 100]),
                ),
                tooltip=[alt.Tooltip("total_energy:Q", title="energy", format=".0f")],
            )
            .properties(height=150, title="Energy reserve")
        )

    if not panels:
        return _empty(line=True, title="HR · load · energy — no rows")
    return panels[0] if len(panels) == 1 else alt.vconcat(*panels)

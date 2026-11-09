"""Tests for the Feature-4 table-browser builders and chart helpers in
``dashboard.metrics``.

The builders take a ``query`` callable (``SQL -> DataFrame``), so they are
exercised with a fake query that records the SQL and simulates the ``ORDER BY
... DESC`` + ``LIMIT`` the DB would apply — no real database needed. This pins
the SQL the dashboard runs (filters, cap, no stray ``AND``) and the post-query
transforms (``spo2_pct``, row cap), which are the consumer-visible contracts.
"""

from __future__ import annotations

import re

import pandas as pd
import pytest

from dashboard import metrics as m


# --- fakes ------------------------------------------------------------------


def fake_query(rows: pd.DataFrame):
    """A ``query`` callable that records SQL and emulates ``ORDER BY <col>
    DESC`` + ``LIMIT N`` so cap/order behaviour is observable without a DB."""
    calls: list[str] = []

    def query(sql: str) -> pd.DataFrame:
        calls.append(sql)
        df = rows.copy()
        order = re.search(r"order by (\w+) desc", sql, re.IGNORECASE)
        if order and order.group(1) in df.columns:
            df = df.sort_values(order.group(1), ascending=False)
        limit = re.search(r"\blimit (\d+)", sql, re.IGNORECASE)
        if limit:
            df = df.head(int(limit.group(1)))
        return df

    return query, calls


def _ppi_rows(n: int = 5) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "time": [f"2026-01-01 00:00:0{i}" for i in range(n)],
            "hr": [60 + i for i in range(n)],
            "spo2": [950, 0, 880, 900, 0][:n],
            "ppi": [1000, 980, 760, 800, 1500][:n],
            "ppi_clean": [1000.0, None, 760.0, 800.0, 1500.0][:n],
            "activity": ["RELAXED"] * n,
            "fresh": [True] * n,
        }
    )


# --- window_cutoff / last_data_time ----------------------------------------


def test_window_cutoff_resolves_presets_relative_to_anchor():
    anchor = pd.Timestamp("2026-06-01 12:00:00")
    assert m.window_cutoff("1h", anchor=anchor) == anchor - pd.Timedelta(hours=1)
    assert m.window_cutoff("6h", anchor=anchor) == anchor - pd.Timedelta(hours=6)
    assert m.window_cutoff("24h", anchor=anchor) == anchor - pd.Timedelta(hours=24)
    assert m.window_cutoff("7d", anchor=anchor) == anchor - pd.Timedelta(days=7)
    assert m.window_cutoff("All", anchor=anchor) is None


def test_window_cutoff_unknown_preset_defaults_to_one_hour():
    anchor = pd.Timestamp("2026-06-01 12:00:00")
    assert m.window_cutoff("bogus", anchor=anchor) == anchor - pd.Timedelta(hours=1)


def test_window_cutoff_no_anchor_falls_back_to_now():
    cutoff = m.window_cutoff("1h")
    assert cutoff is not None
    assert cutoff.tz is None
    now = pd.Timestamp.now()
    assert abs((now - cutoff).total_seconds() - 3600.0) < 5.0


def test_last_data_time_returns_max_or_none():
    # max(time) over one device's layer, used to anchor a window to the data
    def q_max(sql: str) -> pd.DataFrame:
        assert "max(" in sql.lower() and "device_id = 1" in sql
        return pd.DataFrame({"t": [pd.Timestamp("2026-10-29 20:35:07")]})

    t = m.last_data_time(q_max, 1, "ppi", "time")
    assert t == pd.Timestamp("2026-10-29 20:35:07")

    # empty layer -> None (window then falls back to "now")
    def q_empty(sql: str) -> pd.DataFrame:
        return pd.DataFrame({"t": [pd.NaT]})

    assert m.last_data_time(q_empty, 1, "ppi", "time") is None


def test_window_anchored_to_last_data_time_populates_historical_device():
    # The regression: a device whose last sample is days old must still show its
    # most recent hour under the "1h" default, not an empty future window.
    last = pd.Timestamp("2026-10-29 20:35:07")
    anchor = last  # as last_data_time would return it
    cutoff = m.window_cutoff("1h", anchor=anchor)
    assert cutoff == last - pd.Timedelta(hours=1)
    # "All" is unaffected (no lower bound)
    assert m.window_cutoff("All", anchor=anchor) is None


# --- cleaned_ppi ------------------------------------------------------------


def test_cleaned_ppi_builds_bounded_sql():
    query, calls = fake_query(_ppi_rows())
    cutoff = pd.Timestamp("2026-01-01 00:00:00")
    m.cleaned_ppi(query, 1, cutoff=cutoff, cap=500, cleaned_only=True, fresh_only=True)
    sql = calls[0].lower()
    assert "device_id = 1" in sql
    assert "time > '2026-01-01 00:00:00.000000'" in calls[0]  # exact quoted literal
    assert "ppi_clean is not null" in sql
    assert "fresh = 1" in sql
    assert "order by time desc" in sql
    assert "limit 500" in sql


def test_cleaned_ppi_omits_clauses_when_not_requested():
    query, calls = fake_query(_ppi_rows())
    m.cleaned_ppi(query, 1)  # defaults: no cutoff, no cleaned_only, no fresh_only
    sql = calls[0].lower()
    assert "time >" not in sql
    assert "ppi_clean is not null" not in sql
    assert "fresh = 1" not in sql
    assert "limit 2000" in sql  # default cap
    assert "order by time desc" in sql


def test_cleaned_ppi_applies_spo2_pct_and_caps():
    rows = _ppi_rows(5)
    query, _ = fake_query(rows)
    out = m.cleaned_ppi(query, 1, cap=3, cleaned_only=False, fresh_only=False)
    # cap keeps the 3 most recent (desc order): 00:00:02, 00:00:03, 00:00:04
    assert len(out) == 3
    assert list(out.columns) == [
        "time", "hr", "spo2_pct", "ppi", "ppi_clean", "activity", "fresh",
    ]
    # spo2 raw×10 -> percent for reported rows
    reported = out[out["spo2_pct"].notna()]
    assert set(reported["spo2_pct"]) == {88.0, 90.0}
    # a spo2=0 row that survives the cap is blank, not 0.0
    row04 = out.loc[out["time"] == "2026-01-01 00:00:04", "spo2_pct"]
    assert pd.isna(row04.iloc[0])


def test_cleaned_ppi_empty_input_returns_empty():
    query, _ = fake_query(_ppi_rows().iloc[0:0])
    out = m.cleaned_ppi(query, 1)
    assert out.empty


# --- window_table -----------------------------------------------------------


def test_window_table_filters_and_caps():
    rows = pd.DataFrame(
        {
            "night_date": ["2026-01-01"] * 5,
            "window_start": [f"2026-01-01 0{i}:00:00" for i in range(5)],
            "window_mid_h": [float(i) for i in range(5)],
            "state": ["sympathetic", "SYMPATHETIC", "transition", "invalid", "parasympathetic"],
            "is_sleep": [True, True, False, False, False],
            "valid_ratio": [0.9, 0.8, 0.5, 0.1, 0.7],
            "rmssd_ms": [20.0] * 5,
            "ln_rmssd": [3.0] * 5,
            "lf_hf": [2.0] * 5,
            "lf": [200.0] * 5,
            "hf": [100.0] * 5,
        }
    )
    query, calls = fake_query(rows)
    out = m.window_table(
        query,
        1,
        cutoff=pd.Timestamp("2025-12-31 00:00:00"),
        cap=3,
        states=["sympathetic", "transition"],
        sleep_only=True,
        min_valid=0.5,
    )
    sql = calls[0]
    assert "device_id = 1" in sql
    assert "window_start > '2025-12-31 00:00:00.000000'" in sql
    # case-insensitive state match
    assert "lower(state) in" in sql.lower()
    assert "'sympathetic'" in sql.lower() and "'transition'" in sql.lower()
    assert "is_sleep = 1" in sql
    assert "valid_ratio >= 0.500000" in sql
    assert "limit 3" in sql
    assert len(out) == 3


def test_window_table_min_valid_defaults_to_zero():
    query, calls = fake_query(pd.DataFrame())
    m.window_table(query, 1, cap=100)
    assert "valid_ratio >= 0.000000" in calls[0]


# --- minute_table -----------------------------------------------------------


def test_minute_table_no_clauses_has_no_stray_and():
    rows = pd.DataFrame(
        {
            "timestamp": [f"2026-01-01 00:0{i}:00" for i in range(3)],
            "night_date": ["2026-01-01"] * 3,
            "heart_rate": [70.0, 71.0, 72.0],
            "s1_load": [1.0, 2.0, 3.0],
            "tss_load": [0.0] * 3,
            "total_load": [1.0, 2.0, 3.0],
            "heart_rate_zone": [0, 0, 0],
            "steps": [0] * 3,
            "calories": [0.0] * 3,
        }
    )
    query, calls = fake_query(rows)
    out = m.minute_table(query, 1, cap=2)
    sql = calls[0]
    # the no-filter case: where has only the device predicate, no trailing "and"
    assert "from minutemetric where device_id = 1 order by timestamp desc limit 2" in sql
    assert len(out) == 2


# --- daily_table ------------------------------------------------------------


def test_daily_table_selects_curated_columns_no_limit():
    rows = pd.DataFrame({"summary_date": ["2026-01-01", "2025-12-31"]})
    query, calls = fake_query(rows)
    out = m.daily_table(query, 1)
    sql = calls[0]
    low = sql.lower()
    # the curated subset is pulled in
    for col in [
        "status_score", "tsb", "sympathetic_pct", "boom_go_text",
        "training_suggestion", "is_temporary_algorithm", "energy_end_f",
    ]:
        assert col in low
    # 7-day baselines / non-curated columns are NOT dragged in
    for col in ["hrv_night_avg", "hrv_morning_avg", "deduction_points", "bonus_points"]:
        assert col not in low
    # no time filter, no cap — every day is shown
    assert "order by summary_date desc" in low
    assert "limit" not in low
    assert "from dailysummary where device_id = 1 order by summary_date desc" in low
    # the builder passes the query result through unchanged
    assert len(out) == 2


# --- rmssd_segment_table ----------------------------------------------------


def test_rmssd_segment_table_builds_bounded_sql():
    rows = pd.DataFrame(
        {
            "night_date": ["2026-01-01"] * 3,
            "segment_id": [1, 2, 3],
            "start_h": [15.7, 17.4, 18.8],
            "end_h": [17.4, 18.8, 21.0],
            "duration_min": [99.0, 79.0, 133.0],
            "window_count": [100, 71, 135],
            "trend_type": ["STABLE", "RISING", "FALLING"],
            "lnrmssd_start": [2.9, 2.65, 3.2],
            "lnrmssd_end": [2.86, 3.18, 3.83],
            "lnrmssd_fitted_start": [2.9, 2.65, 3.2],
            "lnrmssd_fitted_end": [2.86, 3.18, 3.83],
            "lnrmssd_delta": [0.018, 0.412, 0.526],
            "lnrmssd_slope_per_h": [0.001, 0.3, 0.4],
            "segment_volatility": [0.143, 0.101, 0.115],
            "segment_sse": [1.0, 1.0, 1.0],
        }
    )
    query, calls = fake_query(rows)
    out = m.rmssd_segment_table(
        query, 1, cutoff=pd.Timestamp("2025-12-31 12:00:00"), cap=2
    )
    sql = calls[0]
    assert "device_id = 1" in sql
    # night_date is a DATE, so the time-window cutoff is applied at day granularity
    assert "night_date >= '2025-12-31'" in sql
    # the curated subset is pulled in
    for col in [
        "segment_id", "start_h", "end_h", "window_count", "trend_type",
        "lnrmssd_start", "lnrmssd_end", "lnrmssd_delta",
        "lnrmssd_slope_per_h", "segment_volatility", "segment_sse",
    ]:
        assert col in sql
    # the primary key is not dragged in
    assert " id," not in sql and "select id" not in sql.lower()
    assert "order by night_date desc, segment_id asc" in sql
    assert "limit 2" in sql
    assert len(out) == 2


def test_rmssd_segment_table_no_clause_no_stray_and():
    query, calls = fake_query(pd.DataFrame())
    m.rmssd_segment_table(query, 1, cap=50)
    sql = calls[0]
    # the no-filter case: only the device predicate, no trailing "and"
    assert (
        "from rmssdsegment where device_id = 1 "
        "order by night_date desc, segment_id asc limit 50" in sql
    )
    assert "night_date >=" not in sql
    assert "night_date =" not in sql


def test_rmssd_segment_table_day_filter_adds_calendar_predicate():
    query, calls = fake_query(pd.DataFrame())
    m.rmssd_segment_table(query, 1, day="2026-01-01", cap=100)
    sql = calls[0]
    assert "night_date = '2026-01-01'" in sql


def test_rmssd_segment_table_day_filter_omitted_when_none():
    query, calls = fake_query(pd.DataFrame())
    m.rmssd_segment_table(query, 1, cap=100)
    sql = calls[0]
    assert "night_date =" not in sql
    assert "night_date >=" not in sql


# --- hourly_status_table ----------------------------------------------------


def test_hourly_status_table_builds_bounded_sql():
    rows = pd.DataFrame(
        {
            "hour_start": [f"2026-01-01 0{i}:00:00.000000" for i in range(3)],
            "morning_status": [60] * 3,
            "current_status": [60, 55, 50],
            "t1": [0.0] * 3,
            "t2": [1.0, 2.0, 3.0],
            "t3": [0.0] * 3,
            "t4": [0.0] * 3,
            "hr_5min_avg": [60.0, 58.0, 56.0],
            "is_sleep_or_exercise": [True, False, False],
            "is_temporary": [True] * 3,
        }
    )
    query, calls = fake_query(rows)
    out = m.hourly_status_table(
        query, 1, cutoff=pd.Timestamp("2025-12-31 00:00:00"), cap=2
    )
    sql = calls[0]
    assert "device_id = 1" in sql
    # a true datetime time column, so the cutoff is the fixed-width timestamp
    assert "hour_start > '2025-12-31 00:00:00.000000'" in sql
    for col in [
        "morning_status", "current_status", "t1", "t2", "t3", "t4",
        "hr_5min_avg", "is_sleep_or_exercise", "is_temporary",
    ]:
        assert col in sql
    assert "order by hour_start desc" in sql
    assert "limit 2" in sql
    assert len(out) == 2


def test_hourly_status_table_no_clause_no_stray_and_default_cap():
    query, calls = fake_query(pd.DataFrame())
    m.hourly_status_table(query, 1)  # default: no cutoff, cap 5000
    sql = calls[0]
    # the no-filter case: only the device predicate, no trailing "and", big cap
    assert "from hourlystatus where device_id = 1 order by hour_start desc limit 5000" in sql
    assert "hour_start >" not in sql
    assert "date(hour_start)" not in sql


def test_hourly_status_table_day_filter_adds_calendar_predicate():
    query, calls = fake_query(pd.DataFrame())
    m.hourly_status_table(query, 1, day="2026-01-01", cap=100)
    sql = calls[0]
    assert "date(hour_start) = '2026-01-01'" in sql


def test_hourly_status_table_day_filter_omitted_when_none():
    query, calls = fake_query(pd.DataFrame())
    m.hourly_status_table(query, 1, cap=100)
    sql = calls[0]
    assert "date(hour_start)" not in sql


# --- hr_load_energy_table / hr_load_energy_chart ---------------------------


def test_hr_load_energy_table_builds_bounded_sql():
    query, calls = fake_query(_make_hr_load_energy_chart_df())
    cutoff = pd.Timestamp("2026-11-07 00:00:00")
    out = m.hr_load_energy_table(query, 1, cutoff=cutoff, cap=500)
    sql = calls[0]
    assert "device_id = 1" in sql
    assert "timestamp > '2026-11-07 00:00:00.000000'" in sql  # exact quoted literal
    # the three figure series are pulled; per-window / per-second cols are not
    low = sql.lower()
    for col in ["timestamp", "night_date", "heart_rate", "total_load", "total_energy"]:
        assert col in low
    for col in ["s1_load", "tss_load", "heart_rate_zone", "steps", "calories"]:
        assert col not in low
    assert "from minutemetric where device_id = 1" in sql
    assert "order by timestamp desc" in sql
    assert "limit 500" in sql
    assert len(out) == 6  # 6 rows < cap 500 -> all returned


def test_hr_load_energy_table_no_clause_no_stray_and_default_cap():
    query, calls = fake_query(_make_hr_load_energy_chart_df())
    m.hr_load_energy_table(query, 1)  # default: no cutoff, cap 50000
    sql = calls[0]
    # the no-filter case: only the device predicate, no trailing "and", big cap
    assert "from minutemetric where device_id = 1 order by timestamp desc limit 50000" in sql
    assert "timestamp >" not in sql


def test_hr_load_energy_chart_splits_load_per_night():
    # The load panel is one line layer per night session, so the daily reset
    # reads as a sawtooth break (not a false connecting drop). Two nights in the
    # fixture -> 3 panels (HR, load, energy); the load panel holds 2 layers.
    chart = m.hr_load_energy_chart(_make_hr_load_energy_chart_df())
    panels = chart.to_dict()["vconcat"]
    assert [p.get("title") for p in panels] == [
        "Heart rate",
        "Intraday cumulative load (resets daily)",
        "Energy reserve",
    ]
    load_panel = panels[1]
    assert "layer" in load_panel, "load should be one layer per night session"
    assert len(load_panel["layer"]) == 2


def test_hr_load_energy_chart_energy_only_partial_frame_no_night_date():
    # A partial frame lacking night_date must not crash: one energy panel, no
    # break, and no NaN (the JSON-render contract the suite pins).
    df = pd.DataFrame(
        {
            "timestamp": ["2026-11-07 09:00:00", "2026-11-07 09:01:00"],
            "total_energy": [90, 80],
        }
    )
    chart = m.hr_load_energy_chart(df)
    spec = chart.to_dict()
    assert "vconcat" not in spec  # a single panel returns the chart, not a concat
    assert spec.get("title") == "Energy reserve"
    assert "NaN" not in chart.to_json(allow_nan=False)


# --- chart helpers render to JSON (no NaN crashes) --------------------------


def _make_ppi_chart_df():
    return pd.DataFrame(
        {
            "time": ["2026-01-01 00:00:00", "2026-01-01 00:00:01", "2026-01-01 00:00:02"],
            "hr": [60, 61, 62],
            "spo2_pct": [95.0, 96.0, 97.0],
            "ppi": [1000, 980, 960],
            "ppi_clean": [1000.0, None, 960.0],
            "activity": ["RELAXED"] * 3,
            "fresh": [True] * 3,
        }
    )


def _make_window_chart_df():
    return pd.DataFrame(
        {
            "night_date": ["2026-01-01"] * 3,
            "window_start": [f"2026-01-01 0{i}:00:00" for i in range(3)],
            "window_mid_h": [0.0, 0.5, 1.0],
            "state": ["sympathetic", "transition", "parasympathetic"],
            "is_sleep": [True, True, False],
            "valid_ratio": [0.9, 0.8, 0.7],
            "rmssd_ms": [20.0, 18.0, 22.0],
            "ln_rmssd": [3.0, 2.9, 3.1],
            "lf_hf": [2.0, 1.5, 0.8],
            "lf": [200.0, 190.0, 180.0],
            "hf": [100.0, 100.0, 100.0],
        }
    )


def _make_minute_chart_df():
    return pd.DataFrame(
        {
            "timestamp": [f"2026-01-01 00:0{i}:00" for i in range(3)],
            "night_date": ["2026-01-01"] * 3,
            "heart_rate": [70.0, 71.0, 72.0],
            "s1_load": [1.0, 2.0, 3.0],
            "tss_load": [0.0] * 3,
            "total_load": [1.0, 2.0, 3.0],
            "heart_rate_zone": [0, 1, 0],
            "steps": [0, 10, 20],
            "calories": [0.0, 1.0, 2.0],
        }
    )


def _make_daily_chart_df():
    return pd.DataFrame(
        {
            "summary_date": ["2026-01-01", "2025-12-31"],
            "status_score": [60, 55],
            "base_score": [50, 50],
            "sleep_score": [50, 45],
            "rest_heart_rate_val": [60.0, 61.0],
            "hrv_night_val": [50.0, 48.0],
            "hrv_morning_val": [40.0, 39.0],
            "spo2_night_avg": [950.0, 945.0],
            "spo2_night_min": [900.0, 890.0],
            "energy_start_e": [1.0, 1.0],
            "energy_end_f": [0.7, 0.6],
            "total_load": [100.0, 90.0],
            "total_tss": [5.0, 4.0],
            "atl": [30.0, 28.0],
            "ctl": [50.0, 48.0],
            "tsb": [-18.0, -20.0],
            "sympathetic_pct": [40.0, 35.0],
            "parasympathetic_pct": [30.0, 25.0],
            "transition_pct": [30.0, 40.0],
            "boom_go_text": ["go", "go"],
            "training_suggestion": ["easy", "easy"],
            "is_temporary_algorithm": [True, True],
        }
    )


def _make_hr_load_energy_chart_df():
    """Two night sessions of per-minute HR / cumulative load / energy — enough to
    exercise the load panel's per-night-session break (the sawtooth reset)."""
    return pd.DataFrame(
        {
            "timestamp": [
                "2026-11-07 09:00:00", "2026-11-07 09:01:00", "2026-11-07 09:02:00",
                "2026-11-08 09:00:00", "2026-11-08 09:01:00", "2026-11-08 09:02:00",
            ],
            "night_date": ["2026-11-07", "2026-11-07", "2026-11-07",
                           "2026-11-08", "2026-11-08", "2026-11-08"],
            "heart_rate": [50.0, 120.0, 60.0, 48.0, 110.0, 55.0],
            "total_load": [1.0, 30.0, 60.0, 2.0, 35.0, 70.0],
            "total_energy": [100, 90, 80, 95, 85, 75],
        }
    )


def _make_rmssd_chart_df():
    """A night of RMSSD macro-segments with an uppercase ``trend_type`` (as the
    DB stores it) — enough to exercise the trajectory chart and its lowercasing."""
    return pd.DataFrame(
        {
            "night_date": ["2026-10-12"] * 3,
            "segment_id": [1, 2, 3],
            "start_h": [15.7, 17.4, 18.8],
            "end_h": [17.4, 18.8, 21.0],
            "duration_min": [99.0, 79.0, 133.0],
            "window_count": [100, 71, 135],
            "trend_type": ["STABLE", "RISING", "RISING"],
            "lnrmssd_start": [2.9, 2.65, 3.2],
            "lnrmssd_end": [2.86, 3.18, 3.83],
            "lnrmssd_fitted_start": [2.9, 2.65, 3.2],
            "lnrmssd_fitted_end": [2.86, 3.18, 3.83],
            "lnrmssd_delta": [0.018, 0.412, 0.526],
            "lnrmssd_slope_per_h": [0.001, 0.3, 0.4],
            "segment_volatility": [0.143, 0.101, 0.115],
            "segment_sse": [1.0, 1.0, 1.0],
        }
    )


def _make_hourly_chart_df():
    return pd.DataFrame(
        {
            "hour_start": [
                "2026-10-13 07:00:00.000000",
                "2026-10-13 14:00:00.000000",
                "2026-10-13 23:00:00.000000",
            ],
            "morning_status": [60, 60, 60],
            "current_status": [60, 55, 50],
            "t1": [0.0] * 3,
            "t2": [1.0, 2.0, 0.0],
            "t3": [0.0] * 3,
            "t4": [0.0] * 3,
            "hr_5min_avg": [60.0, 58.0, 56.0],
            "is_sleep_or_exercise": [True, False, True],
            "is_temporary": [True, True, True],
        }
    )


@pytest.mark.parametrize(
    "df, helper",
    [
        (_make_ppi_chart_df(), m.ppi_clean_chart),
        (_make_window_chart_df(), m.window_ribbon_chart),
        (_make_minute_chart_df(), m.minute_hr_chart),
        (_make_daily_chart_df(), m.daily_autonomic_chart),
        (_make_hr_load_energy_chart_df(), m.hr_load_energy_chart),
        (_make_rmssd_chart_df(), m.rmssd_segment_chart),
        (_make_hourly_chart_df(), m.hourly_status_chart),
    ],
)
def test_charts_render_to_json_on_populated_data(df, helper):
    chart = helper(df)
    # allow_nan=False must not raise — the helpers drop NaN before encoding.
    json = chart.to_json(allow_nan=False)
    assert isinstance(json, str) and len(json) > 0


@pytest.mark.parametrize(
    "cols, helper",
    [
        (["time", "ppi", "ppi_clean"], m.ppi_clean_chart),
        (["window_mid_h", "lf_hf", "state"], m.window_ribbon_chart),
        (["timestamp", "heart_rate", "heart_rate_zone"], m.minute_hr_chart),
        (
            ["summary_date", "sympathetic_pct", "parasympathetic_pct", "transition_pct"],
            m.daily_autonomic_chart,
        ),
        (
            ["timestamp", "night_date", "heart_rate", "total_load", "total_energy"],
            m.hr_load_energy_chart,
        ),
        (
            ["night_date", "segment_id", "start_h", "end_h", "trend_type",
             "lnrmssd_start", "lnrmssd_end"],
            m.rmssd_segment_chart,
        ),
        (
            ["hour_start", "morning_status", "current_status", "t1", "t2", "t3",
             "t4", "hr_5min_avg", "is_sleep_or_exercise", "is_temporary"],
            m.hourly_status_chart,
        ),
    ],
)
def test_charts_render_placeholder_on_empty_data(cols, helper):
    chart = helper(pd.DataFrame(columns=cols))
    json = chart.to_json(allow_nan=False)
    assert isinstance(json, str) and len(json) > 0


def test_rmssd_segment_chart_normalizes_trend_case():
    # Regression: the DB stores TrendType as the enum *name* (uppercase, e.g.
    # "RISING"); the color scale fixes a lowercase domain. Without lowercasing,
    # no plotted point maps to a color and the trajectory renders empty even
    # though the slice is full of segments.
    chart = m.rmssd_segment_chart(_make_rmssd_chart_df())
    j = chart.to_json(allow_nan=False)
    assert "RISING" not in j and "STABLE" not in j and "FALLING" not in j
    for v in ("rising", "falling", "stable"):
        assert v in j


def test_window_ribbon_chart_normalizes_state_case():
    # Regression: the DB stores WindowState as the enum *name* (uppercase, e.g.
    # "PARASYMPATHETIC") — SQLModel's Enum column persists the name and the
    # dashboard reads the raw VARCHAR via pandas, bypassing the enum — while the
    # ribbon's color scale fixes a lowercase domain (the enum *values*). Without
    # normalizing the state, no plotted point maps to a color and Vega renders an
    # empty ribbon even though the window is full of lf_hf windows.
    df = pd.DataFrame(
        {
            "window_mid_h": [1.0, 2.0, 3.0],
            "lf_hf": [0.5, 0.6, 0.55],
            "state": ["PARASYMPATHETIC", "TRANSITION", "SYMPATHETIC"],
        }
    )
    chart = m.window_ribbon_chart(df)
    spec = chart.to_dict()
    domain = set(spec["encoding"]["color"]["scale"]["domain"])
    states = [
        row["state"]
        for rows in spec.get("datasets", {}).values()
        if isinstance(rows, list) and rows and "state" in rows[0]
        for row in rows
    ]
    assert states, "lf_hf windows did not survive into the chart data"
    # every plotted state must map to a color in the fixed lowercase domain
    assert all(s in domain for s in states)


# --- last_lfhf_time / window_ribbon caption (self-explaining empty chart) ----


def test_last_lfhf_time_returns_max_or_none():
    # the most recent window_start carrying an lf_hf, or None when the device
    # has none — used to explain an empty LF/HF ribbon.
    def q_max(sql: str) -> pd.DataFrame:
        assert "lf_hf is not null" in sql.lower() and "window_start" in sql
        assert "device_id = 1" in sql
        return pd.DataFrame({"t": [pd.Timestamp("2026-10-29 18:13:00")]})

    assert m.last_lfhf_time(q_max, 1) == pd.Timestamp("2026-10-29 18:13:00")

    def q_empty(sql: str) -> pd.DataFrame:
        return pd.DataFrame({"t": [pd.NaT]})

    assert m.last_lfhf_time(q_empty, 1) is None


def test_window_ribbon_has_data():
    assert m.window_ribbon_has_data(pd.DataFrame({"lf_hf": [2.0, None, 0.8]}))
    assert not m.window_ribbon_has_data(pd.DataFrame({"lf_hf": [None, None]}))
    assert not m.window_ribbon_has_data(pd.DataFrame(columns=["lf_hf"]))
    assert not m.window_ribbon_has_data(pd.DataFrame(columns=[]))


def test_window_ribbon_caption_explains_empty_window():
    # an all-invalid window: state the window, the counts, and why there is no
    # chart (most recent lf_hf and how long ago it was).
    start = pd.Timestamp("2026-10-29 20:00:00")
    end = pd.Timestamp("2026-10-29 21:00:00")
    df = pd.DataFrame({"window_start": [start], "lf_hf": [None]})
    cap = m.window_ribbon_caption(df, start, end, last_lfhf=pd.Timestamp("2026-10-29 18:13:00"))
    assert "**1** windows" in cap
    assert "**0** with LF/HF" in cap
    assert "no LF/HF in this window" in cap
    assert "18:13" in cap
    assert "before this window" in cap  # the gap branch was taken


def test_window_ribbon_caption_device_without_lfhf():
    start = pd.Timestamp("2026-10-29 20:00:00")
    end = pd.Timestamp("2026-10-29 21:00:00")
    df = pd.DataFrame({"window_start": [start], "lf_hf": [None]})
    cap = m.window_ribbon_caption(df, start, end, last_lfhf=None)
    assert "no LF/HF windows at all" in cap
    assert "before this window" not in cap  # no gap to report


def test_window_ribbon_caption_with_data_has_no_explanation():
    start = pd.Timestamp("2026-10-29 20:00:00")
    end = pd.Timestamp("2026-10-29 21:00:00")
    df = pd.DataFrame({"window_start": [start], "lf_hf": [0.8]})
    cap = m.window_ribbon_caption(df, start, end, last_lfhf=None)
    assert "**1** with LF/HF" in cap
    assert "no LF/HF in this window" not in cap


# --- global day filter ------------------------------------------------------


def test_cleaned_ppi_day_filter_adds_calendar_predicate():
    query, calls = fake_query(_ppi_rows())
    m.cleaned_ppi(query, 1, day="2026-01-01")
    sql = calls[0]
    assert "date(time) = '2026-01-01'" in sql
    assert "device_id = 1" in sql


def test_cleaned_ppi_day_filter_omitted_when_none():
    query, calls = fake_query(_ppi_rows())
    m.cleaned_ppi(query, 1)
    sql = calls[0]
    assert "date(time)" not in sql


def test_window_table_day_filter_adds_calendar_predicate():
    rows = pd.DataFrame(
        {
            "night_date": ["2026-01-01"] * 3,
            "window_start": [f"2026-01-01 0{i}:00:00" for i in range(3)],
            "window_mid_h": [0.0, 1.0, 2.0],
            "state": ["sympathetic"] * 3,
            "is_sleep": [False] * 3,
            "valid_ratio": [1.0] * 3,
            "rmssd_ms": [0.0] * 3,
            "ln_rmssd": [0.0] * 3,
            "lf_hf": [0.0] * 3,
            "lf": [0.0] * 3,
            "hf": [0.0] * 3,
        }
    )
    query, calls = fake_query(rows)
    m.window_table(query, 1, day="2026-01-01", cap=50)
    sql = calls[0]
    assert "date(window_start) = '2026-01-01'" in sql
    assert "valid_ratio >= 0.000000" in sql


def test_minute_table_day_filter_adds_calendar_predicate():
    rows = pd.DataFrame(
        {
            "timestamp": [f"2026-01-01 0{i}:00" for i in range(3)],
            "night_date": ["2026-01-01"] * 3,
            "heart_rate": [70.0, 71.0, 72.0],
            "s1_load": [1.0, 2.0, 3.0],
            "tss_load": [0.0] * 3,
            "total_load": [1.0, 2.0, 3.0],
            "heart_rate_zone": [0] * 3,
            "steps": [0] * 3,
            "calories": [0.0] * 3,
        }
    )
    query, calls = fake_query(rows)
    m.minute_table(query, 1, day="2026-01-01", cap=50)
    assert "date(timestamp) = '2026-01-01'" in calls[0]


def test_minute_table_day_filter_omitted_when_none():
    query, calls = fake_query(pd.DataFrame())
    m.minute_table(query, 1, cap=10)
    assert "date(timestamp)" not in calls[0]


def test_hr_load_energy_table_day_filter_adds_calendar_predicate():
    query, calls = fake_query(_make_hr_load_energy_chart_df())
    m.hr_load_energy_table(query, 1, day="2026-01-01", cap=50)
    assert "date(timestamp) = '2026-01-01'" in calls[0]


def test_daily_table_day_filter_restricts_to_one_day():
    rows = pd.DataFrame({"summary_date": ["2026-01-01", "2025-12-31"]})
    query, calls = fake_query(rows)
    m.daily_table(query, 1, day="2026-01-01")
    sql = calls[0]
    assert "summary_date = '2026-01-01'" in sql
    assert "device_id = 1" in sql
    # still newest-first, still no cap
    assert "order by summary_date desc" in sql
    assert "limit" not in sql


def test_daily_table_day_filter_omitted_when_none():
    query, calls = fake_query(pd.DataFrame({"summary_date": ["2026-01-01"]}))
    m.daily_table(query, 1)
    assert "summary_date =" not in calls[0]


def test_day_bounds_all_is_day_span():
    lower, upper = m.day_bounds("2026-01-01", "All")
    assert lower == pd.Timestamp("2026-01-01 00:00:00")
    assert upper == pd.Timestamp("2026-01-02 00:00:00")


def test_day_bounds_preset_centers_on_day():
    lower, upper = m.day_bounds("2026-01-01", "12h")
    assert lower == pd.Timestamp("2026-01-01 06:00:00")
    assert upper == pd.Timestamp("2026-01-01 18:00:00")


def test_day_bounds_preset_spans_full_day_for_24h():
    # a 24h window centered on noon is exactly the calendar day, midnight to
    # midnight; the per-day calendar predicate in the builders is what keeps a
    # *wider* preset inside the chosen day.
    lower, upper = m.day_bounds("2026-01-01", "24h")
    assert lower == pd.Timestamp("2026-01-01 00:00:00")
    assert upper == pd.Timestamp("2026-01-02 00:00:00")


def test_day_bounds_wide_preset_crosses_midnight():
    # a 30d window centered on the day spills into neighbouring days; the
    # per-day calendar predicate (added by the builders) is what keeps the
    # result inside the chosen day.
    lower, upper = m.day_bounds("2026-01-01", "30d")
    assert lower == pd.Timestamp("2025-12-17 12:00:00")
    assert upper == pd.Timestamp("2026-01-16 12:00:00")


def test_day_bounds_unknown_preset_defaults_to_one_hour():
    lower, upper = m.day_bounds("2026-01-01", "bogus")
    assert lower == pd.Timestamp("2026-01-01 11:30:00")
    assert upper == pd.Timestamp("2026-01-01 12:30:00")


def test_day_bounds_none_day_returns_no_bounds():
    assert m.day_bounds(None, "All") == (None, None)


def _FakePicker(value):
    class _P:
        def __init__(self, v):
            self.value = v
    return _P(value)


def test_day_from_picker_uses_picker_value():
    query, calls = fake_query(pd.DataFrame())
    got = m._day_from_picker(_FakePicker("2026-01-01"), query, 1, "ppi", "time")
    assert got == pd.Timestamp("2026-01-01").date()
    # a concrete value short-circuits: no max(time) lookup is issued
    assert calls == []


def test_day_from_picker_falls_back_to_latest_data_day():
    # No picker value -> fall back to the device's most-recent data day so a
    # freshly loaded device still shows its latest day without a click.
    def q_max(sql: str) -> pd.DataFrame:
        assert "max(" in sql.lower() and "device_id = 1" in sql
        return pd.DataFrame({"t": [pd.Timestamp("2026-10-29 20:35:07")]})

    got = m._day_from_picker(_FakePicker(None), q_max, 1, "ppi", "time")
    assert got == pd.Timestamp("2026-10-29").date()


def test_day_from_picker_falls_back_to_today_when_no_data():
    def q_empty(sql: str) -> pd.DataFrame:
        return pd.DataFrame({"t": [pd.NaT]})

    got = m._day_from_picker(_FakePicker(None), q_empty, 1, "ppi", "time")
    assert got == pd.Timestamp.now().date()


# --- raw_input_quality / coverage_quality (raw-input coverage & quality) ----


def test_raw_input_quality_derives_coverage_and_invalid():
    # one aggregate row: total seconds, active days, null ppi_clean, fresh rows.
    row = pd.DataFrame(
        {
            "total": [1_055_339],
            "active_days": [17],
            "null_clean": [406_795],
            "pending": [0],
        }
    )
    query, calls = fake_query(row)
    q = m.raw_input_quality(query, 1)
    # expected is a full per-second stream over the days the device was active.
    assert q["expected"] == 17 * m.FULL_DAY_S
    assert q["coverage_pct"] == pytest.approx(100.0 * 1_055_339 / (17 * 86_400))
    # no pending -> every null ppi_clean is a processed-but-rejected "invalid".
    assert q["invalid"] == 406_795
    assert q["invalid_pct"] == pytest.approx(100.0 * 406_795 / 1_055_339)
    assert q["valid"] == 1_055_339 - 406_795
    assert q["pending"] == 0
    # device predicate, single aggregate scan (no stray order/limit).
    sql = calls[0].lower()
    assert "device_id = 1" in sql
    assert "count(*) as total" in sql
    assert "count(distinct date(time)) as active_days" in sql
    assert "order by" not in sql
    assert "limit" not in sql


def test_raw_input_quality_excludes_pending_from_invalid():
    # fresh (un-scored) seconds must not read as "invalid": they're pending.
    # 500 null ppi_clean = 300 scored-invalid + 200 still fresh.
    row = pd.DataFrame(
        {
            "total": [1000],
            "active_days": [1],
            "null_clean": [500],
            "pending": [200],
        }
    )
    query, _ = fake_query(row)
    q = m.raw_input_quality(query, 1)
    assert q["pending"] == 200
    assert q["invalid"] == 300  # 500 null - 200 pending
    assert q["invalid_pct"] == pytest.approx(30.0)  # over total, not scored
    assert q["valid"] == 500


def test_raw_input_quality_empty_device_is_zeroed():
    # count(*) = 0: SUMs come back NULL, ratios must be 0, no crash.
    row = pd.DataFrame(
        {"total": [0], "active_days": [0], "null_clean": [None], "pending": [None]}
    )
    query, _ = fake_query(row)
    q = m.raw_input_quality(query, 1)
    assert q["total"] == 0
    assert q["expected"] == 0
    assert q["coverage_pct"] == 0.0
    assert q["invalid"] == 0
    assert q["invalid_pct"] == 0.0


def test_coverage_quality_table_two_rows_and_pending_note():
    q = {
        "total": 1000,
        "active_days": 1,
        "expected": 86_400,
        "coverage_pct": 100.0 * 1000 / 86_400,
        "invalid": 300,
        "valid": 500,
        "pending": 200,
        "invalid_pct": 30.0,
    }
    df = m.coverage_quality_table(q)
    assert list(df.columns) == ["measure", "value", "detail"]
    assert len(df) == 2
    # pending seconds are surfaced in the invalid detail so the denominator
    # (all received) vs the invalid count (scored only) stays honest.
    assert "200 not yet scored" in df.iloc[1]["detail"]
    assert df.iloc[0]["value"].endswith("%")
    assert df.iloc[1]["value"].endswith("%")


def test_coverage_quality_table_empty_when_no_data():
    q = {
        "total": 0,
        "active_days": 0,
        "expected": 0,
        "coverage_pct": 0.0,
        "invalid": 0,
        "valid": 0,
        "pending": 0,
        "invalid_pct": 0.0,
    }
    df = m.coverage_quality_table(q)
    assert df.empty


def test_coverage_quality_md_mentions_both_measures():
    md = m.coverage_quality_md(
        {"active_days": 17, "coverage_pct": 71.9, "invalid_pct": 38.5}
    )
    assert "coverage" in md.lower()
    assert "invalid" in md.lower()
    assert "17 active day(s)" in md

import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def device_sidebar(mo, sql, devices):
    get_device, set_device = mo.state(None)
    current_id = (get_device() or {}).get("id")
    buttons = []
    for _, row in devices.iterrows():
        is_selected = row.get("id") == current_id
        # A leading marker plus a distinct button kind make the active device
        # unmistakable; the marker survives even a re-render that lags the colour.
        label = f"● {row.get('mac', '?')}" if is_selected else str(row.get("mac", "?"))
        btn = mo.ui.button(
            label=label,
            on_click=lambda _, dv=row: set_device(dv),
            kind="primary" if is_selected else "neutral",
        )
        buttons.append(btn)

    sidebar = mo.sidebar(
        [mo.md("### Devices"), mo.vstack(buttons, gap=0.3)], width="200px"
    )
    return get_device, set_device, sidebar


@app.cell
def imports():
    import altair as alt
    import marimo as mo
    import numpy as np
    import pandas as pd
    import sqlalchemy as sql

    return mo, pd, alt, np, sql


@app.cell
def connect_db(sql, pd):

    DATABASE_URL = "sqlite:///data/newboom.db"
    engine = sql.create_engine(DATABASE_URL)
    devices = pd.read_sql("select * from device", engine)
    return (devices, engine)


@app.cell
def query_fn(engine, pd):
    def query(sql: str) -> pd.DataFrame:
        return pd.read_sql(sql, engine)

    return (query,)


@app.cell
def metrics():
    from dashboard import metrics as m

    return (m,)


@app.cell
def controls(mo):
    # Time window shared by the per-second / per-window / per-minute tables.
    window_dd = mo.ui.dropdown(
        ["1h", "6h", "24h", "7d", "30d", "All"], value="1h", label="Time window"
    )
    # Hard row cap for every time-series table (server-side LIMIT).
    row_cap = mo.ui.dropdown(
        [1000, 2000, 5000, 10000], value=2000, label="Max rows"
    )
    # Per-table controls.
    # Defaults are "show everything; filter on demand" so a browser never opens
    # empty: a device may have no cleaned rows yet, or its most-recent windows
    # may all be INVALID (a poor-signal period).
    cleaned_only = mo.ui.switch(value=False, label="cleaned rows only")
    fresh_only = mo.ui.switch(value=False, label="fresh rows only")
    win_states = mo.ui.multiselect(
        ["sympathetic", "parasympathetic", "transition", "invalid"],
        value=["sympathetic", "parasympathetic", "transition", "invalid"],
        label="Window states",
    )
    win_sleep_only = mo.ui.switch(value=False, label="sleep windows only")
    win_min_valid = mo.ui.slider(0.0, 1.0, value=0.0, step=0.05, label="min valid_ratio")
    return (
        window_dd,
        row_cap,
        cleaned_only,
        fresh_only,
        win_states,
        win_sleep_only,
        win_min_valid,
    )


@app.cell
def overview_view(get_device, mo, pd, query, m):
    from dashboard.metrics import fmt_num, fmt_ts

    dv = get_device()
    if dv is None:
        overview_view = mo.md("## Please select a device")
    else:
        device_id = int(dv["id"])
        now = pd.Timestamp.now("UTC").tz_localize(None)  # naive UTC, matches worker

        # 1 — health banner: latest PPI, age, LIVE/STALE, row counts
        meta = m.ingestion_meta(query, device_id)
        banner = mo.md(m.ingestion_summary(meta))

        # 2 — pipeline flow: 6 stages, raw -> derived, live lag per stage
        stages = m.layer_freshness(query, device_id, now=now)
        items = []
        for pos, (_, s) in enumerate(stages.iterrows()):
            rows = fmt_num(int(s["row_count"])) if pd.notna(s["row_count"]) else "0"
            items.append(
                mo.md(
                    f"**{s['stage']}**\n"
                    f"last {fmt_ts(s['latest_data_time'])}\n"
                    f"lag {s['lag']} · {rows} rows"
                )
            )
            if pos < len(stages) - 1:
                items.append(mo.md("→"))
        pipeline = (
            mo.hstack(items, gap=0.2)
            if items
            else mo.md("*no layer data yet*")
        )

        # 3 — task watermarks: progress cursors and their lag back to the raw feed
        prog = m.aggregation_progress(query, device_id)
        prog_table = mo.ui.table(prog[["task", "end_time", "age", "lag_raw"]])

        # 4 — downstream production: windowmetric vs minutemetric rows/day
        prod = m.aggregation_production(query, device_id)
        prod_chart = (
            m.production_chart(prod)
            if not prod.empty
            else mo.md("*no downstream production yet*")
        )

        # 5 — daily coverage: stacked covered/gap per day + top gappy closed days
        daily = m.ingestion_daily(query, device_id)
        coverage = (
            m.coverage_chart(daily)
            if not daily.empty
            else mo.md("*no PPI data yet*")
        )
        gaps = (
            m.ingestion_gaps(daily, meta["last"])
            if not pd.isna(meta["last"])
            else pd.DataFrame(columns=["day", "rows", "gap_pct"])
        )
        gap_table = (
            mo.ui.table(gaps)
            if not gaps.empty
            else mo.md("*no closed-day gaps yet*")
        )

        overview_view = mo.vstack(
            [
                mo.md(f"## {dv['mac']} — ingestion"),
                banner,
                mo.md("### Pipeline"),
                pipeline,
                mo.md("### Task watermarks"),
                prog_table,
                mo.md("### Downstream production"),
                prod_chart,
                mo.md("### Daily coverage"),
                coverage,
                gap_table,
            ]
        )
    return (overview_view,)


@app.cell
def ppi_panel(mo, pd, query, get_device, m, window_dd, row_cap, cleaned_only, fresh_only):
    _dv = get_device()
    if _dv is None:
        ppi_panel = mo.md("*select a device*")
    else:
        _id = int(_dv["id"])
        _anchor = m.last_data_time(query, _id, "ppi", "time")
        _cutoff = m.window_cutoff(window_dd.value, anchor=_anchor)
        _cap = int(row_cap.value)
        _df = m.cleaned_ppi(
            query,
            _id,
            cutoff=_cutoff,
            cap=_cap,
            cleaned_only=bool(cleaned_only.value),
            fresh_only=bool(fresh_only.value),
        )
        _capped = " · *capped — widen window or raise cap to see more*" if len(_df) >= _cap else ""
        _caption = mo.md(
            f"**{len(_df):,} rows** · window {window_dd.value} · "
            f"cleaned-only {cleaned_only.value} · fresh-only {fresh_only.value}{_capped}"
        )
        _chart = m.ppi_clean_chart(_df)
        _table = mo.ui.table(
            _df,
            page_size=100,
            show_search=True,
            show_download=True,
            freeze_columns_left=["time"],
        )
        ppi_panel = mo.vstack([_caption, _chart, _table])
    return (ppi_panel,)


@app.cell
def window_panel(
    mo, pd, query, get_device, m,
    window_dd, row_cap, win_states, win_sleep_only, win_min_valid,
):
    _dv = get_device()
    if _dv is None:
        window_panel = mo.md("*select a device*")
    else:
        _id = int(_dv["id"])
        _anchor = m.last_data_time(query, _id, "windowmetric", "window_start")
        _cutoff = m.window_cutoff(window_dd.value, anchor=_anchor)
        _cap = int(row_cap.value)
        _df = m.window_table(
            query,
            _id,
            cutoff=_cutoff,
            cap=_cap,
            states=list(win_states.value or []),
            sleep_only=bool(win_sleep_only.value),
            min_valid=float(win_min_valid.value or 0.0),
        )
        _last_lfhf = m.last_lfhf_time(query, _id)
        _caption = m.window_ribbon_caption(_df, _cutoff, _anchor, _last_lfhf)
        if len(_df) >= _cap:
            _caption += "  \n_*capped — raise Max rows or widen Time window_*"
        _items = [mo.md(_caption)]
        if m.window_ribbon_has_data(_df):
            _items.append(m.window_ribbon_chart(_df))
        _table = mo.ui.table(
            _df,
            page_size=100,
            show_search=True,
            show_download=True,
            freeze_columns_left=["window_start"],
        )
        _items.append(_table)
        window_panel = mo.vstack(_items)
    return (window_panel,)


@app.cell
def minute_panel(mo, pd, query, get_device, m, window_dd, row_cap):
    _dv = get_device()
    if _dv is None:
        minute_panel = mo.md("*select a device*")
    else:
        _id = int(_dv["id"])
        _anchor = m.last_data_time(query, _id, "minutemetric", "timestamp")
        _cutoff = m.window_cutoff(window_dd.value, anchor=_anchor)
        _cap = int(row_cap.value)
        _df = m.minute_table(query, _id, cutoff=_cutoff, cap=_cap)
        _capped = " · *capped — raise cap or widen window to see more*" if len(_df) >= _cap else ""
        _caption = mo.md(f"**{len(_df):,} rows** · window {window_dd.value}{_capped}")
        _chart = m.minute_hr_chart(_df)
        _table = mo.ui.table(
            _df,
            page_size=100,
            show_search=True,
            show_download=True,
            freeze_columns_left=["timestamp"],
        )
        minute_panel = mo.vstack([_caption, _chart, _table])
    return (minute_panel,)


@app.cell
def daily_panel(mo, pd, query, get_device, m):
    _dv = get_device()
    if _dv is None:
        daily_panel = mo.md("*select a device*")
    else:
        _id = int(_dv["id"])
        _df = m.daily_table(query, _id)
        _caption = mo.md(f"**{len(_df)} daily summary row(s)** · curated columns, newest first")
        _chart = m.daily_autonomic_chart(_df)
        _table = mo.ui.table(
            _df,
            pagination=True,
            page_size=10,
            show_download=True,
            freeze_columns_left=["summary_date"],
        )
        daily_panel = mo.vstack([_caption, _chart, _table])
    return (daily_panel,)


@app.cell
def tables_view(mo, get_device, ppi_panel, window_panel, minute_panel, daily_panel):
    _dv = get_device()
    # The banner keeps the active device visible at the top of this tab, where
    # (unlike Overview) no per-panel header otherwise names it.
    _banner = (
        mo.md(f"### Device: `{_dv['mac']}`  (id {_dv['id']})")
        if _dv is not None
        else mo.md("*Select a device from the left panel.*")
    )
    sub_tabs = mo.ui.tabs(
        {
            "Cleaned PPI": ppi_panel,
            "Windows": window_panel,
            "Minutes": minute_panel,
            "Daily": daily_panel,
        }
    )
    tables_view = mo.vstack([_banner, sub_tabs])
    return (tables_view,)


@app.cell
def _(mo, sidebar, overview_view, tables_view):
    mo.hstack(
        [sidebar, mo.ui.tabs({"Overview": overview_view, "Tables": tables_view})],
        gap=0.5,
    )


if __name__ == "__main__":
    app.run()

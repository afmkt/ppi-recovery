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
            kind="info" if is_selected else "neutral",
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
    # The Load & Energy figure spans two days, so it gets its own window control
    # defaulting to 48h (window_dd above defaults to 1h and has no 48h option).
    rec_window = mo.ui.dropdown(
        ["12h", "24h", "48h", "7d", "30d", "All"], value="48h", label="Time window"
    )
    return (
        window_dd,
        row_cap,
        cleaned_only,
        fresh_only,
        win_states,
        win_sleep_only,
        win_min_valid,
        rec_window,
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
def uploader_mod():
    from dashboard import uploader as uploader

    return (uploader,)


@app.cell
def upload_view(mo, uploader):
    # The dashboard runs in-process in the FastAPI app, so it POSTs to the same
    # server that owns /api/upload/{ppi,sleep}. The endpoint validates the body,
    # writes the hash-named file and upserts by (device_id, time) — the UI only
    # moves bytes. Target + prefix come from the module (env-overridable).
    base = uploader.DEFAULT_BASE
    prefix = uploader.DEFAULT_PREFIX
    status, set_status = mo.state("")
    type_dd = mo.ui.dropdown(["PPI", "Sleep"], value="PPI", label="Type")
    file_in = mo.ui.file(
        filetypes=[".json"],
        kind="area",
        label="Drop or pick a ppi / sleep JSON file",
    )

    def on_upload(_):
        results = file_in.value
        if not results:
            set_status("⚠ Pick a file first.")
            return
        rec = results[0]
        dtype = type_dd.value.lower()
        # Fail fast on malformed JSON instead of a cryptic 422 from the API.
        try:
            uploader.parse_payload(rec.contents)
        except (ValueError, TypeError) as e:
            set_status(f"⚠ {e}")
            return
        res = uploader.post_upload(base, prefix, dtype, rec.contents)
        set_status(res.as_markdown())

    upload_btn = mo.ui.button(label="Upload", on_click=on_upload, kind="neutral")
    hint = mo.md(
        f"POSTs the file verbatim to `{prefix}/{{ppi,sleep}}` on `{base}` — "
        "the same endpoint a device / `newboom_sync.py` uses (validates, writes "
        "the hash-named file, upserts by device+time; idempotent). A **newly "
        "uploaded device** shows in the left panel after a reload (the device "
        "list is read once at connect)."
    )
    _status = status()
    status_md = (
        mo.md(_status)
        if _status
        else mo.md("_Idle — pick a type, choose a file, then Upload._")
    )
    upload_view = mo.vstack(
        [
            mo.md("## Upload ppi / sleep JSON"),
            mo.hstack([type_dd, file_in, upload_btn], gap=0.5),
            status_md,
            hint,
        ]
    )
    return (upload_view,)


@app.cell
def recovery_view(mo, query, get_device, m, rec_window):
    _dv = get_device()
    # ``rec_window`` (its own 48h-default control, created in ``controls``) is
    # passed in so its value is read in a cell other than the one that created
    # it — marimo forbids reading a UIElement's value in its own cell.
    if _dv is None:
        recovery_view = mo.md("*select a device*")
    else:
        _id = int(_dv["id"])
        _anchor = m.last_data_time(query, _id, "minutemetric", "timestamp")
        _cutoff = m.window_cutoff(rec_window.value, anchor=_anchor)
        _cap = 50000
        _df = m.hr_load_energy_table(query, _id, cutoff=_cutoff, cap=_cap)
        _capped = " · *capped — widen window or raise cap to see more*" if len(_df) >= _cap else ""
        _caption = mo.md(
            f"**{len(_df):,} rows** · window {rec_window.value} · "
            "heart rate · intraday load · energy reserve" + _capped
        )
        _chart = m.hr_load_energy_chart(_df)
        _table = mo.ui.table(
            _df,
            page_size=100,
            show_search=True,
            show_download=True,
            freeze_columns_left=["timestamp"],
        )
        recovery_view = mo.vstack([_caption, rec_window, _chart, _table])
    return (recovery_view,)


@app.cell
def _(mo, sidebar, overview_view, tables_view, recovery_view, upload_view):
    mo.hstack(
        [
            sidebar,
            mo.ui.tabs(
                {
                    "Overview": overview_view,
                    "Tables": tables_view,
                    "Load & Energy": recovery_view,
                    "Upload": upload_view,
                }
            ),
        ],
        gap=0.5,
    )


if __name__ == "__main__":
    app.run()

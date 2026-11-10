import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def device_sidebar(mo, devices, now_picker, window_dd):
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

    # The global "now" and "time window" live here, above the device list: they
    # are shared by every tab (Tables sub-tabs and the Load & Energy figure), so
    # one picker + one dropdown drive the whole dashboard. A window is the range
    # [now - window, now]; set "now" back to view a historical device.
    sidebar = mo.sidebar(
        [
            mo.md("### Now"),
            now_picker,
            mo.md("### Time window"),
            window_dd,
            mo.md("### Devices"),
            mo.vstack(buttons, gap=0.3),
        ],
        width="200px",
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
def controls(mo, pd):
    # Global "now": the system clock every panel reads its window from. A full
    # datetime (date + hour/minute/second) so a window can start at any instant,
    # not just a calendar day's noon. Default is wall-clock now; a historical
    # device is viewed by setting "now" back to its range. ``value`` is an
    # ISO-8601 'YYYY-MM-DDTHH:MM:SS' string (naive local, matching stored times).
    now_picker = mo.ui.datetime(
        value=pd.Timestamp.now().strftime("%Y-%m-%dT%H:%M:%S"),
        precision="second",
        label="Now",
    )
    # Global time window shared by *every* graph and data table — the Tables
    # sub-tabs and the Load & Energy figure alike. The range is always
    # [now - window, now] (see metrics.window_bounds). Presets are finite: there
    # is no "All" — the raw PPI table is ~1M rows/device, so an unbounded window
    # can never reach the browser and the cap always binds instead.
    window_dd = mo.ui.dropdown(
        ["1h", "6h", "24h", "48h", "7d", "30d"], value="24h", label="Time window"
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
        now_picker,
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
        now = pd.Timestamp.now()  # naive local, matches stored times

        # 1 — health banner: latest PPI, age, LIVE/STALE, row counts
        meta = m.ingestion_meta(query, device_id)
        banner = mo.md(m.ingestion_summary(meta))

        # 2 — raw-input coverage & quality: the two direct measures of the raw
        # PPI feed — how complete it is (received / expected) and how much of it
        # is unusable (invalid / total). Device-wide headline; the per-day
        # coverage chart lower down drills into completeness per calendar day.
        quality = m.raw_input_quality(query, device_id)
        cq = m.coverage_quality_table(quality)
        cq_view = mo.ui.table(cq) if not cq.empty else mo.md("*no PPI data yet*")

        # 3 — pipeline flow: 6 stages, raw -> derived, live lag per stage
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

        # 4 — task watermarks: progress cursors and their lag back to the raw feed
        prog = m.aggregation_progress(query, device_id)
        prog_table = mo.ui.table(prog[["task", "end_time", "age", "lag_raw"]])

        # 5 — downstream production: windowmetric vs minutemetric rows/day
        prod = m.aggregation_production(query, device_id)
        prod_chart = (
            m.production_chart(prod)
            if not prod.empty
            else mo.md("*no downstream production yet*")
        )

        # 6 — daily coverage: stacked covered/gap per day + top gappy closed days
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
                mo.md("### Coverage & data quality"),
                mo.md(m.coverage_quality_md(quality)),
                cq_view,
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
def ppi_panel(mo, pd, query, get_device, m, now_picker, window_dd, row_cap, cleaned_only, fresh_only):
    _dv = get_device()
    if _dv is None:
        ppi_panel = mo.md("*select a device*")
    else:
        _id = int(_dv["id"])
        _lower, _upper = m.window_bounds(now_picker.value, window_dd.value)
        _cap = int(row_cap.value)
        _df = m.cleaned_ppi(
            query,
            _id,
            cutoff=_lower,
            upper=_upper,
            cap=_cap,
            cleaned_only=bool(cleaned_only.value),
            fresh_only=bool(fresh_only.value),
        )
        _capped = "*capped — widen window or raise cap to see more*" if len(_df) >= _cap else ""
        _caption = mo.md(
            f"**{len(_df):,} rows** · "
            + m.window_range_md(
                window_dd.value, _lower, _upper,
                f"cleaned-only {cleaned_only.value}",
                f"fresh-only {fresh_only.value}",
                _capped,
            )
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
    mo, pd, query, get_device, m, now_picker,
    window_dd, row_cap, win_states, win_sleep_only, win_min_valid,
):
    _dv = get_device()
    if _dv is None:
        window_panel = mo.md("*select a device*")
    else:
        _id = int(_dv["id"])
        _lower, _upper = m.window_bounds(now_picker.value, window_dd.value)
        _cap = int(row_cap.value)
        _df = m.window_table(
            query,
            _id,
            cutoff=_lower,
            upper=_upper,
            cap=_cap,
            states=list(win_states.value or []),
            sleep_only=bool(win_sleep_only.value),
            min_valid=float(win_min_valid.value or 0.0),
        )
        _last_lfhf = m.last_lfhf_time(query, _id)
        _caption = m.window_ribbon_caption(_df, _lower, _upper, _last_lfhf)
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
def minute_panel(mo, pd, query, get_device, m, now_picker, window_dd, row_cap):
    _dv = get_device()
    if _dv is None:
        minute_panel = mo.md("*select a device*")
    else:
        _id = int(_dv["id"])
        _lower, _upper = m.window_bounds(now_picker.value, window_dd.value)
        _cap = int(row_cap.value)
        _df = m.minute_table(query, _id, cutoff=_lower, upper=_upper, cap=_cap)
        _capped = "*capped — raise cap or widen window to see more*" if len(_df) >= _cap else ""
        _caption = mo.md(
            f"**{len(_df):,} rows** · "
            + m.window_range_md(window_dd.value, _lower, _upper, _capped)
        )
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
def daily_panel(mo, pd, query, get_device, m, now_picker, window_dd):
    _dv = get_device()
    if _dv is None:
        daily_panel = mo.md("*select a device*")
    else:
        _id = int(_dv["id"])
        _lower, _upper = m.window_bounds(now_picker.value, window_dd.value)
        _df = m.daily_table(query, _id, cutoff=_lower, upper=_upper)
        _caption = mo.md(
            f"**{len(_df)} daily summary row(s)** · "
            + m.window_range_md(window_dd.value, _lower, _upper, "curated columns, newest first")
        )
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
def rmssd_panel(mo, pd, query, get_device, m, now_picker, window_dd, row_cap):
    _dv = get_device()
    if _dv is None:
        rmssd_panel = mo.md("*select a device*")
    else:
        _id = int(_dv["id"])
        _lower, _upper = m.window_bounds(now_picker.value, window_dd.value)
        _cap = int(row_cap.value)
        _df = m.rmssd_segment_table(query, _id, cutoff=_lower, upper=_upper, cap=_cap)
        _capped = "*capped — raise cap or widen window to see more*" if len(_df) >= _cap else ""
        _caption = mo.md(
            f"**{len(_df):,} segments** · "
            + m.window_range_md(
                window_dd.value, _lower, _upper, "ln-RMSSD trajectory by trend", _capped
            )
        )
        _chart = m.rmssd_segment_chart(_df)
        _table = mo.ui.table(
            _df,
            page_size=100,
            show_search=True,
            show_download=True,
            freeze_columns_left=["night_date", "segment_id"],
        )
        rmssd_panel = mo.vstack([_caption, _chart, _table])
    return (rmssd_panel,)


@app.cell
def hourly_panel(mo, pd, query, get_device, m, now_picker, window_dd, row_cap):
    _dv = get_device()
    if _dv is None:
        hourly_panel = mo.md("*select a device*")
    else:
        _id = int(_dv["id"])
        _lower, _upper = m.window_bounds(now_picker.value, window_dd.value)
        _cap = int(row_cap.value)
        _df = m.hourly_status_table(query, _id, cutoff=_lower, upper=_upper, cap=_cap)
        _capped = "*capped — raise cap or widen window to see more*" if len(_df) >= _cap else ""
        _caption = mo.md(
            f"**{len(_df):,} hours** · "
            + m.window_range_md(
                window_dd.value, _lower, _upper, "current vs frozen morning status", _capped
            )
        )
        _chart = m.hourly_status_chart(_df)
        _table = mo.ui.table(
            _df,
            page_size=100,
            show_search=True,
            show_download=True,
            freeze_columns_left=["hour_start"],
        )
        hourly_panel = mo.vstack([_caption, _chart, _table])
    return (hourly_panel,)


@app.cell
def tables_view(mo, get_device, ppi_panel, window_panel, minute_panel, rmssd_panel, hourly_panel, daily_panel):
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
            "RMSSD": rmssd_panel,
            "Hourly": hourly_panel,
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
def upload_view(mo, uploader, pd):
    # The dashboard runs in-process in the FastAPI app, so it POSTs to the same
    # server that owns /api/upload/{ppi,sleep}. The endpoint validates the body,
    # writes the hash-named file and upserts by (device_id, time) — the UI only
    # moves bytes. Target + prefix come from the module (env-overridable).
    base = uploader.DEFAULT_BASE
    prefix = uploader.DEFAULT_PREFIX

    # One file area + button per type, side by side: a PPI box and a Sleep box,
    # so a user never has to choose a type before picking a file.
    #
    # Uploads run synchronously in the button callback. A per-second PPI/sleep
    # capture is small and the transfer to the same host is fast, so the callback
    # blocks only for that transfer — no worker thread. The cost is a brief UI
    # freeze during the transfer, after which the results render, which is exactly
    # the "the UI blocks and then tells me what happened" behaviour we want.
    #
    # Feedback the user can actually read:
    #   * a per-box "last attempt" summary (✅ N/N · rows / ❌ which file + why),
    #   * a shared, newest-first history table of every file's outcome.
    _HISTORY_CAP = 50
    # allow_self_loops=True is required: this cell both *sets* the state (from the
    # button on_click callbacks) and *reads* it, so by marimo's default it would
    # NOT re-render after a click — leaving the user with no feedback. This flag
    # makes the click's state update re-run this cell so the per-box summary and
    # history table update in place.
    _history, _set_history = mo.state([], allow_self_loops=True)
    _last_ppi, _set_last_ppi = mo.state("", allow_self_loops=True)
    _last_sleep, _set_last_sleep = mo.state("", allow_self_loops=True)

    def _record(dtype, results):
        """Append a batch's per-file results to the history (newest first) and
        refresh that type's last-attempt summary line."""
        now = pd.Timestamp.now().strftime("%H:%M:%S")
        rows = [
            {
                "Time": now,
                "Type": r.dtype.upper() if r.dtype else "—",
                "File": r.name or "—",
                "Result": "✅" if r.ok else "❌",
                "Rows": f"{r.processed:,}" if r.processed is not None else "—",
                "Device": f"#{r.device_id}" if r.device_id is not None else "—",
                "Saved": r.file or "—",
                "Detail": r.error or r.message or "—",
            }
            for r in results
        ]
        _set_history((rows + _history())[:_HISTORY_CAP])
        (_set_last_ppi if dtype == "ppi" else _set_last_sleep)(uploader.summarize(results))

    def _make_box(dtype, label, set_last, summary):
        area = mo.ui.file(
            filetypes=[".json"],
            multiple=True,
            kind="area",
            label=f"Drop or pick {label} JSON file(s)",
        )

        def on_click(_):
            # ``value`` is a list of (name, contents); empty when nothing picked.
            files = area.value
            if not files:
                set_last("⚠ Pick a file first.")
                return
            # The whole batch uploads together; each file is gated and reported
            # independently, so one bad file doesn't abort the rest.
            results = uploader.post_upload_batch(
                base,
                prefix,
                dtype,
                [(f.name, f.contents) for f in files],
            )
            _record(dtype, results)

        btn = mo.ui.button(
            label=f"Upload {label}",
            on_click=on_click,
            kind="success",
        )
        summary_md = (
            mo.md(summary)
            if summary
            else mo.md(f"_Idle — pick {label} file(s), then Upload._")
        )
        return mo.vstack(
            [
                mo.md(f"### {label}"),
                area,
                btn,
                summary_md,
            ]
        )

    # Read each box's last-attempt summary at the cell's top level so marimo
    # tracks them as this cell's dependencies: a click updates the state and
    # this cell re-renders to show the new summary / refresh the history table.
    ppi_box = _make_box("ppi", "PPI", _set_last_ppi, _last_ppi())
    sleep_box = _make_box("sleep", "Sleep", _set_last_sleep, _last_sleep())

    # Shared per-file history (newest first), capped so the tab stays readable.
    history = _history()
    if history:
        history_df = pd.DataFrame(
            history,
            columns=["Time", "Type", "File", "Result", "Rows", "Device", "Saved", "Detail"],
        )
        history_view = mo.ui.table(
            history_df,
            page_size=100,
            show_search=True,
            show_download=True,
            freeze_columns_left=["Time", "Type", "File"],
        )
    else:
        history_view = mo.md("_No uploads yet._")

    hint = mo.md(
        f"Each box POSTs its file(s) verbatim to `{prefix}/{{ppi,sleep}}` on `{base}` — "
        "the same endpoint a device / `newboom_sync.py` uses (validates, writes the "
        "hash-named file, upserts by device+time; idempotent). Pick several files at "
        "once and the whole batch uploads together. A **newly uploaded device** shows "
        "in the left panel after a reload (the device list is read once at connect)."
    )

    upload_view = mo.vstack(
        [
            mo.md("## Upload ppi / sleep JSON"),
            mo.hstack([ppi_box, sleep_box], gap=1),
            mo.md("### Recent uploads"),
            history_view,
            hint,
        ]
    )
    return (upload_view,)


@app.cell
def recovery_view(mo, query, get_device, m, now_picker, window_dd):
    _dv = get_device()
    if _dv is None:
        recovery_view = mo.md("*select a device*")
    else:
        _id = int(_dv["id"])
        _lower, _upper = m.window_bounds(now_picker.value, window_dd.value)
        _cap = 50000
        _df = m.hr_load_energy_table(query, _id, cutoff=_lower, upper=_upper, cap=_cap)
        _capped = "*capped — widen window or raise cap to see more*" if len(_df) >= _cap else ""
        _caption = mo.md(
            f"**{len(_df):,} rows** · "
            + m.window_range_md(
                window_dd.value, _lower, _upper,
                "heart rate · intraday load · energy reserve",
                _capped,
            )
        )
        _chart = m.hr_load_energy_chart(_df)
        _table = mo.ui.table(
            _df,
            page_size=100,
            show_search=True,
            show_download=True,
            freeze_columns_left=["timestamp"],
        )
        recovery_view = mo.vstack([_caption, _chart, _table])
    return (recovery_view,)


@app.cell
def docs_view(mo, get_device):
    import html

    from dashboard import boom_docs

    # A static reference: each app screen paired with the endpoint that renders
    # it. Loaded entirely from the committed asset dir src/dashboard/resources
    # (pairs.json + the screenshot PNGs) — the runtime never reads the docx.
    # Regenerate the assets from the docx with tools/extract_boom_images.py.
    # It never shows "select a device": the {address} template in each link is
    # filled with the MAC of the device selected in the sidebar, and falls back
    # to the literal {address} when no device is selected.
    #
    # get_device() is the sidebar's global selection: a pandas row, or None when
    # nothing is selected.
    _dv = get_device()
    _address = _dv.get("mac") if _dv is not None else None

    err = None
    pairs = []
    try:
        pairs = boom_docs.load_pairs()
    except OSError as e:
        err = str(e)

    if err is not None:
        docs_view = mo.md(
            "## API docs\n\n*Pair manifest not found — run `python tools/extract_boom_images.py`.*"
        )
    else:

        def card(p):
            shots = []
            for n in p.images:
                img = boom_docs.image_path(n)
                shots.append(
                    mo.image(src=str(img), width=240, rounded=True, alt=p.title)
                    if img.is_file()
                    else mo.md(f"*{n} missing from resources/*")
                )
            shots_view = mo.hstack(shots, gap=1) if len(shots) > 1 else shots[0]
            # Origin-relative (Pair.path strips the host), so a click lands on the
            # endpoint served by the same instance as the dashboard — whether that
            # is http://localhost:8000 or https://boom.sparkpos.cn. {address} is
            # filled with the selected device's MAC (or left as the template when
            # none is selected). target="_blank" opens the endpoint in a new
            # browser tab instead of navigating away from the dashboard.
            href = boom_docs.fill_address(p.path, _address)
            link = mo.Html(
                f"<strong>{html.escape(p.method)}</strong>&nbsp;&nbsp;"
                f"<a href=\"{html.escape(href, quote=True)}\" target=\"_blank\">"
                f"{html.escape(href)}</a>"
            )
            return mo.vstack([mo.md(p.title), shots_view, link])

        blocks = [
            mo.md(
                "## API docs\n\n"
                "Each screen of the mobile app, paired with the endpoint that renders "
                "it (from `docs/boom.v1.0.docx`). Clicking an endpoint opens it in a "
                "new browser tab on this server — the link is relative to the "
                "dashboard's own origin, with `{address}` filled from the device "
                "selected in the left panel."
            )
        ]
        section = None
        for p in pairs:
            if p.section != section:
                section = p.section
                blocks.append(mo.md(f"### {section}"))
            blocks.append(card(p))
        docs_view = mo.vstack(blocks)
    return (docs_view,)


@app.cell
def _(mo, sidebar, overview_view, tables_view, recovery_view, upload_view, docs_view):
    mo.hstack(
        [
            sidebar,
            mo.ui.tabs(
                {
                    "Overview": overview_view,
                    "Tables": tables_view,
                    "Load & Energy": recovery_view,
                    "Upload": upload_view,
                    "API Docs": docs_view,
                }
            ),
        ],
        gap=0.5,
    )


if __name__ == "__main__":
    app.run()

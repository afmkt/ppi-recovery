import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def imports():
    import marimo as mo
    import pandas as pd
    import sqlalchemy as sql

    from dashboard import metrics

    return mo, pd, sql, metrics


@app.cell
def connect_db(sql, pd):
    """Open the ingest DB and expose a `query` helper (SQL -> DataFrame)."""
    DATABASE_URL = "sqlite:///data/newboom.db"
    engine = sql.create_engine(DATABASE_URL)
    devices = pd.read_sql("select * from device order by id", engine)

    def query(sql_text: str) -> "pd.DataFrame":
        return pd.read_sql(sql_text, engine)

    return devices, engine, query


@app.cell
def device_sidebar(mo, devices):
    get_device, set_device = mo.state(None)

    current = get_device() or {}
    buttons = [
        mo.ui.button(
            label="◀  All devices",
            on_click=lambda _: set_device(None),
            kind="info" if current.get("id") is None else "secondary",
        )
    ]
    for _, row in devices.iterrows():
        selected = row["id"] == current.get("id")
        buttons.append(
            mo.ui.button(
                label=row["name"],
                on_click=lambda _, dv=row: set_device(dv),
                kind="info" if selected else "success",
            )
        )

    sidebar = mo.sidebar(
        [mo.md("### Devices"), mo.vstack(buttons, gap=0.3)],
        width="300px",
    )
    return get_device, set_device, sidebar


@app.cell
def selected_device(get_device, devices):
    """Resolve the active selection to a full device row, or None."""
    dv = get_device()
    if dv is None:
        device = None
    else:
        hit = devices.loc[devices["id"] == dv["id"]]
        device = hit.iloc[0] if not hit.empty else None
    return (device,)


# --- Feature 1: ingestion health (fleet overview, always shown) -------------


@app.cell
def fleet_section(query, devices, metrics, mo):
    _fresh = metrics.fleet_freshness(query, devices)
    _chart = metrics.throughput_chart(metrics.fleet_throughput(query))
    _cols = ["mac", "name", "last_ppi", "age", "status",
             "active_days", "ppi_rows", "fresh_pct"]
    _table = mo.ui.table(_fresh[_cols].copy(), page_size=10)
    fleet_view = mo.vstack([
        mo.md(metrics.fleet_header(_fresh)),
        mo.hstack([_table, mo.vstack([mo.md("#### PPI throughput / day"), _chart])], gap=0.6),
    ])
    return (fleet_view,)


# --- Feature 1: per-device ingestion throughput + gaps ----------------------


@app.cell
def ingestion_section(query, device, metrics, mo):
    if device is None:
        ing_view = None
    else:
        _did = int(device["id"])
        _meta = metrics.ingestion_meta(query, _did)
        _daily = metrics.ingestion_daily(query, _did)
        _gaps = metrics.ingestion_gaps(_daily, _meta["last"])
        _chart = metrics.throughput_chart(_daily)
        ing_view = mo.vstack([
            mo.md(metrics.ingestion_summary(_meta)),
            mo.vstack([mo.md("#### Rows / day (red line = full 86 400s)"), _chart]),
            mo.vstack([
                mo.md("#### Largest coverage gaps (closed days)"),
                mo.ui.table(_gaps[["day", "rows", "gap_pct"]].copy(), page_size=5),
            ]),
        ])
    return (ing_view,)


# --- Feature 2: aggregation pipeline monitoring -----------------------------


@app.cell
def aggregation_section(query, device, metrics, mo):
    if device is None:
        agg_view = None
    else:
        _did = int(device["id"])
        _prog = metrics.aggregation_progress(query, _did)
        _prod = metrics.aggregation_production(query, _did)
        _chart = metrics.production_chart(_prod)
        _raw_max = _prog["end_time"].max() if not _prog.empty else None
        _table = mo.ui.table(
            _prog[["task", "end_time", "age", "lag_raw"]].copy(), page_size=10
        )
        agg_view = mo.vstack([
            mo.md(metrics.aggregation_summary(_raw_max)),
            _table,
            mo.vstack([mo.md("#### Production / day"), _chart]),
        ])
    return (agg_view,)


# --- Feature 3: data quality & statistical sanity ---------------------------


@app.cell
def quality_section(query, device, metrics, mo):
    if device is None:
        qual_view = None
    else:
        _did = int(device["id"])
        _s = metrics.quality_metrics(query, _did)
        _checks = metrics.quality_checks(_s)
        _win = metrics.window_summary(query, _did)
        _daily = metrics.daily_summary(query, _did)

        _checks_view = mo.ui.table(_checks.copy(), page_size=10)
        _state_chart = metrics.state_chart(_win["states"])
        qual_view = mo.vstack([
            mo.md(metrics.quality_ranges_md(_s)),
            _checks_view,
            mo.hstack([
                mo.vstack([mo.md("#### Window states"), _state_chart]),
                mo.md(metrics.window_quality_md(_win["quality"], _win["total"])),
            ], gap=0.6),
            mo.vstack([
                mo.md("#### Daily summary (recent)"),
                mo.ui.table(_daily.copy(), page_size=14),
            ]),
        ])
    return (qual_view,)


@app.cell
def compose_detail(mo, device, ing_view, agg_view, qual_view):
    """Compose the per-device detail; prompt when nothing is selected."""
    if device is None:
        detail_view = mo.md("_Select a device on the left to inspect its pipeline._")
    else:
        _sections = [
            mo.md(f"## {device['mac']}  ·  {device['name']}  _tz {device['tz']}_"),
            ing_view,
            agg_view,
            qual_view,
        ]
        detail_view = mo.vstack([s for s in _sections if s is not None], gap=0.6)
    return (detail_view,)


@app.cell
def _(mo, sidebar, fleet_view, detail_view):
    mo.hstack(
        [
            sidebar,
            mo.vstack(
                [
                    mo.md("# NewBoom — Pipeline & Data Dashboard"),
                    fleet_view,
                    detail_view,
                ],
                gap=0.4,
            ),
        ],
        gap=0.5,
    )


if __name__ == "__main__":
    app.run()

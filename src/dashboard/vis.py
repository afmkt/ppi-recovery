import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def device_sidebar(mo, sql, devices):
    get_device, set_device = mo.state(None)
    buttons = []
    current = get_device() or {}
    for _, row in devices.iterrows():
        label = row.get("mac", "?")
        btn = mo.ui.button(
            label=label,
            on_click=lambda _, dv=row: set_device(dv),
            kind="info" if row.get("id") == current.get("id") else "success",
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
def view_device(get_device, mo, pd, engine):
    from dashboard import metrics as m
    from dashboard.metrics import fmt_num, fmt_ts

    def query(sql: str) -> pd.DataFrame:
        return pd.read_sql(sql, engine)

    dv = get_device()
    if dv is None:
        device_view = mo.md("## Please select a device")
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

        device_view = mo.vstack(
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
    return (device_view,)


@app.cell
def _(mo, sidebar, device_view):
    mo.hstack([sidebar, device_view], gap=0.5)


if __name__ == "__main__":
    app.run()

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
        [mo.md("### Devices"), mo.vstack(buttons, gap=0.3)], width="280px"
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
def view_device(get_device, mo):
    _dv = get_device()
    if _dv is None:
        device_view = mo.md("## Please select a device")
    else:
        device_view = mo.md(f"## {_dv['mac']}")
    return (device_view,)


@app.cell
def _(mo, sidebar, device_view):
    mo.hstack([sidebar, device_view], gap=0.5)


if __name__ == "__main__":
    app.run()

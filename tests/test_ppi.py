"""Contract for the legacy raw PPI read — ``GET /devices/{address}/ppi``.

Pins the compat-shim behavior: the raw-list envelope (NOT ``BoomResponse``),
per-device filtering, chronological order, the six legacy fields, ``spo2``
passed through on the device-raw ×10 scale, a blank ``timezone`` when the device
has none, and a 404 for an unknown device.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from newboom.boom.ppi import get_device_ppi
from newboom.database import engine
from newboom.model import PPI, ActivityState, Device

_MAC = "FE:9A:75:B4:01:AA"
_TZ = "08:00"
_SECONDS = (22, 23, 24)


def _seed(device_tz: str | None = _TZ) -> int:
    """Insert one device plus three per-second PPI rows; return the device id.

    The last row carries ``spo2 == 0`` (device did not report that second), so
    the pass-through scale assertion distinguishes 0 from "not 95".
    """
    with Session(engine) as s:
        device = Device(name="t", mac=_MAC, tz=device_tz)
        s.add(device)
        s.commit()
        s.refresh(device)
        dev_id = int(device.id)
        s.add_all(
            PPI(
                device_id=dev_id,
                time=datetime(2026, 10, 14, 3, 57, sec),
                hr=60 + sec,
                spo2=950 if sec != 24 else 0,
                ppi=938 + sec,
                activity=ActivityState.DEEP_SLEEP,
                fresh=True,
            )
            for sec in _SECONDS
        )
        s.commit()
    return dev_id


def _call(mac: str, **kw) -> list:
    with Session(engine) as s:
        return asyncio.run(
            get_device_ppi(
                mac,
                start_time=kw.get("start_time"),
                end_time=kw.get("end_time"),
                limit=kw.get("limit", 50000),
                session=s,
            )
        )


def test_ppi_is_raw_list_not_envelope():
    _seed()
    rows = _call(_MAC)
    assert isinstance(rows, list)
    # exactly the six legacy fields, no BoomResponse wrapping
    for r in rows:
        assert set(r) == {
            "time",
            "hr",
            "spo2",
            "ppi",
            "device_address",
            "timezone",
        }


def test_ppi_chronological_order_and_fields():
    _seed()
    rows = _call(_MAC)
    assert [r["time"] for r in rows] == [f"2026-10-14 03:57:{s:02d}" for s in _SECONDS]
    first = rows[0]
    assert first["device_address"] == _MAC
    assert first["timezone"] == _TZ


def test_ppi_spo2_passes_through_raw_x10_scale():
    # 950 (95.0%) is returned as-is; 0 ("not reported that second") is kept,
    # never folded to a percentage.
    _seed()
    rows = _call(_MAC)
    assert rows[0]["spo2"] == 950
    assert rows[-1]["spo2"] == 0


def test_ppi_timezone_blank_when_unset():
    _seed(device_tz=None)
    rows = _call(_MAC)
    assert rows[0]["timezone"] == ""


def test_ppi_time_filter():
    _seed()
    rows = _call(_MAC, start_time=datetime(2026, 10, 14, 3, 57, 23))
    assert [r["time"] for r in rows] == ["2026-10-14 03:57:23", "2026-10-14 03:57:24"]


def test_ppi_limit_keeps_most_recent_chronological():
    # desc + limit + reverse => the N most recent, oldest-first.
    _seed()
    rows = _call(_MAC, limit=2)
    assert [r["time"] for r in rows] == ["2026-10-14 03:57:23", "2026-10-14 03:57:24"]


def test_ppi_empty_window_returns_empty_list():
    _seed()
    rows = _call(_MAC, start_time=datetime(2020, 1, 1), end_time=datetime(2020, 1, 2))
    assert rows == []


def test_ppi_unknown_device_404():
    _seed()
    with Session(engine) as s:
        with pytest.raises(HTTPException) as ei:
            asyncio.run(get_device_ppi("AA:BB:CC:DD:EE:FF", None, None, 10, s))
        assert ei.value.status_code == 404

"""Legacy raw PPI read — GET /devices/{address}/ppi.

Compat shim for the retired ``boom_api`` ``/devices/{address}/ppi`` endpoint.
Unlike the rest of the boom read surface, this returns a **raw JSON list** (not
a ``BoomResponse`` envelope) so the existing frontends (``boom_ui`` and the
caddy-served SPA) keep working unchanged.

``spo2`` is the device-raw ×10 value, consistent with the ``PPI`` table and
``/upload/ppi`` (new-interface.md §3.1: ``950`` = 95.0%); the display layer
folds it back to a percentage. ``address`` keys on ``Device.mac``. ``time`` is
the UTC instant as stored (``PPI.time``), rendered ``YYYY-MM-DD HH:MM:SS`` — the
same format as ``/upload/ppi``, which ``boom_ui`` parses with
``pd.to_datetime(…, utc=True)``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, col, select

from newboom.database import get_session
from newboom.model import PPI, Device

router = APIRouter(tags=["PPI 原始数据"])

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


@router.get("/devices/{address}/ppi")
async def get_device_ppi(
    address: str,
    start_time: datetime | None = Query(None, description="起始时刻，不传则不限"),
    end_time: datetime | None = Query(None, description="结束时刻，不传则不限"),
    limit: int = Query(default=50000, ge=1, description="返回的最大记录数"),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """Raw per-second PPI for a device, chronological.

    Mirrors the retired ``boom_api`` ``/devices/{address}/ppi``: the raw
    ``PPI`` rows for ``address`` within ``[start_time, end_time]`` (each bound
    optional), newest-first from the DB then reversed to chronological, capped
    at ``limit``. Missing device → 404; an empty window → ``[]`` (not 404).
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    query = select(PPI).where(PPI.device_id == device.id)
    if start_time:
        query = query.where(PPI.time >= start_time)
    if end_time:
        query = query.where(PPI.time <= end_time)
    query = query.order_by(col(PPI.time).desc()).limit(limit)
    rows = list(reversed(session.exec(query).all()))  # chronological

    tz = device.tz or ""
    return [
        {
            "time": r.time.strftime(_TIME_FMT),
            "hr": r.hr,
            "spo2": r.spo2,
            "ppi": r.ppi,
            "device_address": device.mac,
            "timezone": tz,
        }
        for r in rows
    ]

"""6.x 超量恢复页面 — GET /devices/{address}/{superrecovery,atlctl}."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlmodel import Session, col, select

from newboom.boom.schemas import AtlCtlPoint, BoomResponse, StaleFields
from newboom.boom.staleness import locf_atlctl, mark_stale
from newboom.config import STALE_WINDOW_DAYS
from newboom.database import get_session
from newboom.model import DailySummary, Device

router = APIRouter(tags=["超量恢复"])


class SuperRecoveryData(StaleFields):
    tsb: int = Field(default=0, description="TSB 训练状态值")
    atl: int = Field(default=0, description="短期负荷值")
    ctl: int = Field(default=0, description="长期负荷值")


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


@router.get(
    "/devices/{address}/superrecovery", response_model=BoomResponse[SuperRecoveryData]
)
async def get_superrecovery(
    address: str, session: Session = Depends(get_session)
) -> BoomResponse[SuperRecoveryData]:
    """6.1 获取超量恢复基础数据.

    Returns the most recent finalized day's training-stress triple: ``atl`` /
    ``ctl`` are the 7-/42-day EMAs of daily total load and ``tsb = atl - ctl``
    (``boom_compute.finalize_*``, stored on ``DailySummary``). Only closed days
    carry these, so the latest row with a non-null ``atl`` is used; a missing
    device / no finalized day yields a zeroed payload with ``asOf=None`` /
    ``stale=True``. With a summary, ``asOf`` is its ``summary_date`` and
    ``stale`` True when older than ``STALE_WINDOW_DAYS``.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=SuperRecoveryData(asOf=None, stale=True))

    summary = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device.id)
        .where(col(DailySummary.atl).isnot(None))
        .order_by(col(DailySummary.summary_date).desc())
    ).first()
    if summary is None:
        return BoomResponse(code=0, data=SuperRecoveryData(asOf=None, stale=True))

    as_of, stale = mark_stale(summary.summary_date, device=device)
    return BoomResponse(
        code=0,
        data=SuperRecoveryData(
            tsb=round(summary.tsb or 0),
            atl=round(summary.atl or 0),
            ctl=round(summary.ctl or 0),
            asOf=as_of,
            stale=stale,
        ),
    )


def _atlctl_points(
    session: Session, device_id: int, start: date, end: date
) -> list[AtlCtlPoint]:
    """Per-day ``atl`` / ``ctl`` (LOCF-filled, marked ``carried``). Fetches from
    ``start - STALE_WINDOW_DAYS`` so :func:`locf_atlctl` can seed a leading
    carry, then clips back to ``[start, end]``; a day without a finalized pair
    is carried from the last real one across a gap <= the window, else omitted."""
    rows = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device_id)
        .where(DailySummary.summary_date >= start - timedelta(days=STALE_WINDOW_DAYS))
        .where(DailySummary.summary_date <= end)
        .where(col(DailySummary.atl).isnot(None))
        .order_by(col(DailySummary.summary_date).asc())
    ).all()
    real = [
        AtlCtlPoint(
            date=r.summary_date.isoformat(),
            atl=round(r.atl or 0),
            ctl=round(r.ctl or 0),
        )
        for r in rows
    ]
    return locf_atlctl(real, start, end)


@router.get("/devices/{address}/atlctl", response_model=BoomResponse[list[AtlCtlPoint]])
async def get_atlctl(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[AtlCtlPoint]]:
    """6.2 获取 ATL/CTL 趋势数据.

    Per-day ``atl`` / ``ctl`` over ``[startDate, endDate]``; a day without a
    finalized value is carried from the last real pair across a gap <=
    ``STALE_WINDOW_DAYS`` (marked ``carried``), else omitted. A missing device
    yields ``[]``.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=[])

    start = _parse_date(startDate)
    end = _parse_date(endDate)
    if end < start:
        start, end = end, start
    return BoomResponse(
        code=0,
        data=_atlctl_points(
            session, device.id if device.id is not None else -1, start, end
        ),
    )

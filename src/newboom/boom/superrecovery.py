"""6.x 超量恢复页面 — GET /devices/{address}/{superrecovery,atlctl}."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from newboom.boom.schemas import BoomResponse
from newboom.database import get_session
from newboom.model import DailySummary, Device

router = APIRouter(tags=["超量恢复"])


class SuperRecoveryData(BaseModel):
    tsb: int = Field(default=0, description="TSB 训练状态值")
    atl: int = Field(default=0, description="短期负荷值")
    ctl: int = Field(default=0, description="长期负荷值")


class AtlCtlPoint(BaseModel):
    date: str = Field(..., description="日期 YYYY-MM-DD")
    atl: int = Field(..., description="短期负荷值")
    ctl: int = Field(..., description="长期负荷值")


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
    device / no finalized day yields a zeroed payload.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=SuperRecoveryData())

    summary = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device.id)
        .where(col(DailySummary.atl).isnot(None))
        .order_by(col(DailySummary.summary_date).desc())
    ).first()
    if summary is None:
        return BoomResponse(code=0, data=SuperRecoveryData())

    return BoomResponse(
        code=0,
        data=SuperRecoveryData(
            tsb=round(summary.tsb or 0),
            atl=round(summary.atl or 0),
            ctl=round(summary.ctl or 0),
        ),
    )


def _atlctl_points(
    session: Session, device_id: int, start: date, end: date
) -> list[AtlCtlPoint]:
    """Per-day ``atl`` / ``ctl`` over ``[start, end]``; days without a finalized
    value (``atl`` null, i.e. not yet closed) are omitted."""
    rows = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device_id)
        .where(DailySummary.summary_date >= start)
        .where(DailySummary.summary_date <= end)
        .where(col(DailySummary.atl).isnot(None))
        .order_by(col(DailySummary.summary_date).asc())
    ).all()
    return [
        AtlCtlPoint(
            date=r.summary_date.isoformat(),
            atl=round(r.atl or 0),
            ctl=round(r.ctl or 0),
        )
        for r in rows
    ]


@router.get("/devices/{address}/atlctl", response_model=BoomResponse[list[AtlCtlPoint]])
async def get_atlctl(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[AtlCtlPoint]]:
    """6.2 获取 ATL/CTL 趋势数据.

    Per-day ``atl`` / ``ctl`` over ``[startDate, endDate]``; days without a
    finalized value are omitted. A missing device yields ``[]``.
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

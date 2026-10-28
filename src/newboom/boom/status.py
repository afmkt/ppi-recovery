"""2.x 状态页面 — GET /devices/{address}/{status,readiness}."""

from __future__ import annotations

from datetime import date, time, timedelta
from datetime import datetime as _dt

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from newboom.boom.schemas import BoomResponse, DatePoint, TimePoint
from newboom.config import READINESS_GRANULARITY
from newboom.database import get_session
from newboom.model import DailySummary, Device, HourlyStatus, WindowMetric

router = APIRouter(tags=["状态"])


class StatusData(BaseModel):
    totalStatus: int = Field(0, description="综合状态值 0-100")
    statusProgress: int = Field(0, description="状态进度百分比 0-100")
    hrv: int = Field(0, description="心率变异性 ms（夜间rmssd均值）")
    restingHeartRate: int = Field(0, description="静息心率 bpm")
    hrvData: list[TimePoint] = Field(
        default_factory=list, description="当日HRV变化数据"
    )


def _parse_date(value: str) -> date:
    return _dt.strptime(value, "%Y-%m-%d").date()


def _hrv_data_for_day(
    session: Session, device_id: int, day: date
) -> list[TimePoint]:
    """That night's 5-min RMSSD windows as intraday HH:mm points.

    ``WindowMetric`` is computed at a 300 s window / 60 s step
    (``compute.py``) and keyed by ``night_date`` (``night_key``,
    ~08:00→08:00); this is the intraday HRV curve for the night of
    ``day``. ``TimePoint`` holds one value, so the spec's ±std band is
    not represented.
    """
    rows = session.exec(
        select(WindowMetric)
        .where(WindowMetric.device_id == device_id)
        .where(WindowMetric.night_date == day)
        .where(col(WindowMetric.rmssd_ms).isnot(None))
        .order_by(col(WindowMetric.window_start).asc())
    ).all()
    return [
        TimePoint(
            time=w.window_start.strftime("%H:%M"),
            value=round(w.rmssd_ms) if w.rmssd_ms is not None else 0,
        )
        for w in rows
    ]


@router.get(
    "/devices/{address}/status", response_model=BoomResponse[StatusData]
)
async def get_status(
    address: str,
    date: str | None = Query(None, description="查询日期 YYYY-MM-DD，不传默认当天"),
    session: Session = Depends(get_session),
) -> BoomResponse[StatusData]:
    """2.1 获取状态页面基础数据.

    Reads the precomputed ``DailySummary`` for the day: the overall
    status (``status_current`` — the latest hourly-adjusted value, frozen
    to ``status_score`` for a closed day) drives ``totalStatus`` /
    ``statusProgress``; ``hrv`` is the night RMSSD mean
    (``hrv_night_val``) and ``restingHeartRate`` the sleep-derived resting
    HR (``rest_heart_rate_val``). ``hrvData`` is that night's 5-min RMSSD
    windows. When ``date`` is omitted the device's most recent day is
    used; a missing device / day yields a zeroed payload
    (``BoomResponse`` carries no error field).
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=StatusData())

    if date:
        target = _parse_date(date)
        summary = session.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device.id)
            .where(DailySummary.summary_date == target)
        ).first()
    else:
        summary = session.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device.id)
            .order_by(col(DailySummary.summary_date).desc())
        ).first()
    if summary is None:
        return BoomResponse(code=0, data=StatusData())

    total_status = (
        summary.status_current
        if summary.status_current is not None
        else (summary.status_score or 0)
    )
    hrv = (
        round(summary.hrv_night_val)
        if summary.hrv_night_val is not None
        else 0
    )
    rhr = (
        round(summary.rest_heart_rate_val)
        if summary.rest_heart_rate_val is not None
        else 0
    )
    hrv_data = _hrv_data_for_day(session, device.id, summary.summary_date)
    return BoomResponse(
        code=0,
        data=StatusData(
            totalStatus=total_status,
            statusProgress=total_status,
            hrv=hrv,
            restingHeartRate=rhr,
            hrvData=hrv_data,
        ),
    )


def _daily_readiness(
    session: Session, device_id: int, start: date, end: date
) -> list[DatePoint]:
    """Per-day frozen status (``DailySummary.status_score``); days with no
    summary or null status are omitted (no fallback)."""
    rows = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device_id)
        .where(DailySummary.summary_date >= start)
        .where(DailySummary.summary_date <= end)
        .where(col(DailySummary.status_score).isnot(None))
        .order_by(col(DailySummary.summary_date).asc())
    ).all()
    return [
        DatePoint(date=r.summary_date.isoformat(), value=r.status_score or 0)
        for r in rows
    ]


def _hourly_readiness(
    session: Session, device_id: int, start: date, end: date
) -> list[DatePoint]:
    """Per-day max intraday hourly status (``HourlyStatus.current_status``).

    Each day's value is the highest status snapshot that day (boom §2.2 "取每天
    最高的状态值"); days with no hourly snapshot are omitted (no fallback).
    """
    start_dt = _dt.combine(start, time.min)
    end_dt = _dt.combine(end, time.min) + timedelta(days=1)
    rows = session.exec(
        select(HourlyStatus)
        .where(HourlyStatus.device_id == device_id)
        .where(HourlyStatus.hour_start >= start_dt)
        .where(HourlyStatus.hour_start < end_dt)
        .where(col(HourlyStatus.current_status).isnot(None))
        .order_by(col(HourlyStatus.hour_start).asc())
    ).all()
    best: dict[date, int] = {}
    for r in rows:
        day = r.hour_start.date()
        value = r.current_status or 0
        best[day] = max(best.get(day, value), value)
    return [
        DatePoint(date=day.isoformat(), value=best[day])
        for day in sorted(best)
    ]


@router.get(
    "/devices/{address}/readiness",
    response_model=BoomResponse[list[DatePoint]],
)
async def get_readiness(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[DatePoint]]:
    """2.2 获取训练准备度数据（用状态值替代）.

    The readiness chart substitutes the status value (boom §2.2 "取每天最高的状态
    值"). ``READINESS_GRANULARITY`` selects the per-day source: ``"hourly"``
    (default) takes each day's max intraday hourly status; ``"daily"`` takes
    the frozen daily status. No fallback between sources — a day with no data
    from the selected source is omitted.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=[])

    start = _parse_date(startDate)
    end = _parse_date(endDate)
    if end < start:
        start, end = end, start

    if READINESS_GRANULARITY == "daily":
        points = _daily_readiness(session, device.id, start, end)
    else:
        points = _hourly_readiness(session, device.id, start, end)
    return BoomResponse(code=0, data=points)

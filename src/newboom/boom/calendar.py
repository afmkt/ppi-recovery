"""8.x 日历图表 — GET /devices/{address}/calendar_tables."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, col, select

from newboom.boom import energy, load, sleep, status
from newboom.boom.schemas import BoomResponse, TimePoint
from newboom.database import get_session
from newboom.model import Device, HourlyStatus

router = APIRouter(tags=["日历"])


# --- helpers ---------------------------------------------------------------


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _review_points(session: Session, device_id: int, day: date) -> list[TimePoint]:
    """The day's intraday status/recovery curve — ``HourlyStatus.current_status``
    per local hour, as ``HH:00`` points in chronological order.

    The calendar ``review`` tab ("超量恢复趋势数据") is the day's recovery
    evolution. The model carries supercompensation (ATL/CTL/TSB) only as a
    per-day summary (:mod:`newboom.boom.superrecovery` §6.2) with no intraday
    series, so the intraday status is the closest intraday representation;
    hours without a snapshot are omitted.
    """
    start_dt = datetime.combine(day, time.min)
    end_dt = start_dt + timedelta(days=1)
    rows = session.exec(
        select(HourlyStatus)
        .where(HourlyStatus.device_id == device_id)
        .where(HourlyStatus.hour_start >= start_dt)
        .where(HourlyStatus.hour_start < end_dt)
        .where(col(HourlyStatus.current_status).isnot(None))
        .order_by(col(HourlyStatus.hour_start).asc())
    ).all()
    return [
        TimePoint(
            time=r.hour_start.strftime("%H:%M"), value=round(r.current_status or 0)
        )
        for r in rows
    ]


def _calendar_points(
    session: Session, device_id: int, day: date, tab: str
) -> list[TimePoint]:
    """The selected day's intraday ``HH:mm`` chart for ``tab`` (boom §8.1).

    Each tab reuses the intraday series the same-page endpoint builds, so the
    calendar can never drift from its source page:

    * ``energy`` — the day's energy curve ``F_(d,t)`` (§4.1 ``energyChartData``).
    * ``load``   — the day's cumulative total-load curve
      (§5.1 ``realtimeLoadData``).
    * ``status`` — that night's 5-min RMSSD windows (§2.1 ``hrvData``).
    * ``sleep``  — the night's sleep-window heart rate (§3.2 ``heartRate``).
    * ``review`` — the day's intraday status/recovery curve
      (see :func:`_review_points`).

    A day with no data for the tab yields ``[]``; an unknown ``tab`` yields
    ``[]`` (mirrors the per-page "unknown metric -> empty" convention, e.g.
    §7.1 ``body_metrics``).
    """
    if tab == "energy":
        return energy._energy_chart_data(session, device_id, day)
    if tab == "load":
        return load._realtime_load(load._minutes(session, device_id, day))
    if tab == "status":
        return status._hrv_data_for_day(session, device_id, day)
    if tab == "sleep":
        return sleep._sleep_metric_points(session, device_id, day, "heartRate")
    if tab == "review":
        return _review_points(session, device_id, day)
    return []


# --- endpoints -------------------------------------------------------------


@router.get(
    "/devices/{address}/calendar_tables", response_model=BoomResponse[list[TimePoint]]
)
async def get_calendar_tables(
    address: str,
    date: str = Query(..., description="查询日期 YYYY-MM-DD"),
    tab: str = Query(..., description="图表类型：energy/load/status/sleep/review"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[TimePoint]]:
    """8.1 获取日历图表数据.

    Returns the selected day's intraday ``HH:mm`` curve for the requested
    ``tab`` (see :func:`_calendar_points`); each tab reuses its source page's
    series so the calendar stays consistent with that page. A missing device
    yields ``[]``.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=[])

    day = _parse_date(date)
    return BoomResponse(
        code=0,
        data=_calendar_points(
            session, device.id if device.id is not None else -1, day, tab
        ),
    )

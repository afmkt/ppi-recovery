"""4.x 储能页面 — GET /devices/{address}/energy_*."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from newboom.boom.schemas import BoomResponse, DatePoint, TimePoint
from newboom.config import FIRST_ENERGY
from newboom.database import get_session
from newboom.model import DailySummary, Device, MinuteMetric

router = APIRouter(tags=["储能"])


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


class EnergyStatusData(BaseModel):
    totalEnergy: int = Field(0, description="总储能值 0-100")
    energyProgress: int = Field(0, description="储能进度百分比 0-100")
    totalCharge: int = Field(0, description="总充能值")
    totalConsume: int = Field(0, description="总耗能值")
    energyChartData: list[TimePoint] = Field(
        default_factory=list, description="当日能量曲线数据"
    )


def _charge_consume(e_d: float, f_d: float) -> tuple[int, int]:
    """Provisional §4.1 ``totalCharge`` / ``totalConsume`` from the day's
    energy (``E_d`` / ``F_d``). The spec names these
    (总充能值 / 总耗能值) but gives no formula, so the mapping is isolated
    here for easy change: for now ``totalCharge`` is the morning starting
    energy ``E_d`` and ``totalConsume`` is the day's consumption
    ``E_d - F_d``, both as 0-100 product scores.
    """
    charge = round(100 * max(0.0, min(1.0, e_d)))
    consume = round(100 * max(0.0, min(1.0, e_d - f_d)))
    return charge, consume


def _energy_chart_data(
    session: Session, device_id: int, day: date
) -> list[TimePoint]:
    """That night's per-minute energy as intraday HH:mm points.

    One point per ``MinuteMetric.total_energy`` (the F_(d,t) product score),
    keyed by ``night_date`` (~08:00→08:00), ordered chronologically.
    """
    rows = session.exec(
        select(MinuteMetric)
        .where(MinuteMetric.device_id == device_id)
        .where(MinuteMetric.night_date == day)
        .order_by(col(MinuteMetric.timestamp).asc())
    ).all()
    return [
        TimePoint(
            time=m.timestamp.strftime("%H:%M"),
            value=m.total_energy,
        )
        for m in rows
    ]


@router.get(
    "/devices/{address}/energy_status",
    response_model=BoomResponse[EnergyStatusData],
)
async def get_energy_status(
    address: str,
    date: str | None = Query(None, description="查询日期 YYYY-MM-DD，不传默认当天"),
    session: Session = Depends(get_session),
) -> BoomResponse[EnergyStatusData]:
    """4.1 获取储能页面基础数据.

    Reads the precomputed ``DailySummary`` for the day.
    ``totalEnergy`` / ``energyProgress`` is the day-end remaining energy
    ``F_d`` (``energy_end_f``); an incomplete day (``F_d`` not finalized)
    retains the last valid minute energy (spec §8 "保留最近一次有效
    totalEnergy"). ``totalCharge`` / ``totalConsume`` come from
    :func:`_charge_consume`; ``energyChartData`` is the day's per-minute
    energy curve. A missing device / day yields a zeroed payload.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=EnergyStatusData())

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
        return BoomResponse(code=0, data=EnergyStatusData())

    chart = _energy_chart_data(session, device.id, summary.summary_date)
    e_d = (
        summary.energy_start_e
        if summary.energy_start_e is not None
        else FIRST_ENERGY
    )
    f_d = summary.energy_end_f
    if f_d is None:
        f_d = chart[-1].value / 100.0 if chart else e_d

    total_energy = round(100 * max(0.0, min(1.0, f_d)))
    charge, consume = _charge_consume(e_d, f_d)
    return BoomResponse(
        code=0,
        data=EnergyStatusData(
            totalEnergy=total_energy,
            energyProgress=total_energy,
            totalCharge=charge,
            totalConsume=consume,
            energyChartData=chart,
        ),
    )


def _energy_trend(
    session: Session, device_id: int, start: date, end: date
) -> list[DatePoint]:
    """Per-day body energy ``F_d``; days without a finalized ``energy_end_f``
    are omitted."""
    rows = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device_id)
        .where(DailySummary.summary_date >= start)
        .where(DailySummary.summary_date <= end)
        .where(col(DailySummary.energy_end_f).isnot(None))
        .order_by(col(DailySummary.summary_date).asc())
    ).all()
    return [
        DatePoint(
            date=r.summary_date.isoformat(),
            value=round(100 * max(0.0, min(1.0, r.energy_end_f))),
        )
        for r in rows
    ]


@router.get(
    "/devices/{address}/energy_trend",
    response_model=BoomResponse[list[DatePoint]],
)
async def get_energy_trend(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[DatePoint]]:
    """4.2 获取身体储能趋势数据.

    Per-day body energy value (``F_d`` = ``energy_end_f``, 0-100) over
    ``[startDate, endDate]``; days without a finalized energy are omitted.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=[])

    start = _parse_date(startDate)
    end = _parse_date(endDate)
    if end < start:
        start, end = end, start
    points = _energy_trend(session, device.id, start, end)
    return BoomResponse(code=0, data=points)


"""7.x 身体指标页面 — GET /devices/{address}/body_metrics."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from newboom.boom.schemas import BoomResponse, DatePoint
from newboom.database import get_session
from newboom.model import DailySummary, Device, MinuteMetric

router = APIRouter(tags=["身体指标"])


class BodyMetricsData(BaseModel):
    currentData: str = Field(default="", description="当前数据值")
    normalRange: str = Field(default="", description="正常范围")
    trendData: list[DatePoint] = Field(default_factory=list, description="趋势数据列表")
    analysisText: str = Field(default="", description="分析建议文本")
    updateTime: str = Field(default="", description="数据更新时间 HH:mm")


# --- helpers ---------------------------------------------------------------


# metric -> (DailySummary float field, divisor). ``heartRate`` has no per-day
# field, so it is computed from ``MinuteMetric`` separately (``_heart_rate_trend``
# / ``_latest_heart_rate``).  ``bloodOxygen`` divides by 10 because
# ``PPI.spo2`` / ``spo2_night_avg`` is the device-raw value (×10, ``950`` = 95.0%);
# only the display layer folds it back to a percentage (``new-interface.md``).
_SUMMARY_METRICS: dict[str, tuple[str, float]] = {
    "restingHeartRate": ("rest_heart_rate_val", 1.0),
    "hrv": ("hrv_night_val", 1.0),
    "bloodOxygen": ("spo2_night_avg", 10.0),
}

_UNITS: dict[str, str] = {
    "heartRate": "bpm",
    "restingHeartRate": "bpm",
    "hrv": "ms",
    "bloodOxygen": "%",
}


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _summary_values(
    session: Session,
    device_id: int,
    start: date,
    end: date,
    field: str,
    divisor: float,
) -> list[DatePoint]:
    """Per-day value from a ``DailySummary`` float field over ``[start, end]``
    (days with a null value omitted), divided by ``divisor`` and rounded."""
    column = col(getattr(DailySummary, field))
    rows = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device_id)
        .where(DailySummary.summary_date >= start)
        .where(DailySummary.summary_date <= end)
        .where(column.isnot(None))
        .order_by(col(DailySummary.summary_date).asc())
    ).all()
    return [
        DatePoint(
            date=r.summary_date.isoformat(), value=round(getattr(r, field) / divisor)
        )
        for r in rows
    ]


def _heart_rate_trend(
    session: Session, device_id: int, start: date, end: date
) -> list[DatePoint]:
    """Per-day mean heart rate (``MinuteMetric.heart_rate`` over each
    ``night_date``); a day with no minutes is omitted."""
    rows = session.exec(
        select(MinuteMetric)
        .where(MinuteMetric.device_id == device_id)
        .where(MinuteMetric.night_date >= start)
        .where(MinuteMetric.night_date <= end)
        .order_by(col(MinuteMetric.timestamp).asc())
    ).all()
    by_day: dict[date, list[float]] = {}
    for m in rows:
        if m.heart_rate is None:
            continue
        by_day.setdefault(m.night_date, []).append(m.heart_rate)
    return [
        DatePoint(date=d.isoformat(), value=round(sum(by_day[d]) / len(by_day[d])))
        for d in sorted(by_day)
    ]


def _latest_summary_value(
    session: Session, device_id: int, field: str, divisor: float
) -> float | None:
    """The most recent day's value from a ``DailySummary`` field, or ``None``."""
    row = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device_id)
        .where(col(getattr(DailySummary, field)).isnot(None))
        .order_by(col(DailySummary.summary_date).desc())
    ).first()
    if row is None:
        return None
    return getattr(row, field) / divisor


def _latest_heart_rate(session: Session, device_id: int) -> float | None:
    """The most recent ``MinuteMetric`` heart rate, or ``None``."""
    row = session.exec(
        select(MinuteMetric)
        .where(MinuteMetric.device_id == device_id)
        .order_by(col(MinuteMetric.timestamp).desc())
    ).first()
    if row is None or row.heart_rate is None:
        return None
    return row.heart_rate


def _analysis_text(session: Session, device_id: int) -> str:
    """The latest day's coaching text (``DailySummary.boom_go_text``), else ``""``."""
    row = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device_id)
        .where(col(DailySummary.boom_go_text).isnot(None))
        .order_by(col(DailySummary.summary_date).desc())
    ).first()
    return row.boom_go_text if row and row.boom_go_text else ""


def _format_current(value: float | None, metric: str) -> str:
    """``"<value><unit>"`` (e.g. ``59bpm``, ``95%``, ``59ms``); ``""`` if absent."""
    if value is None:
        return ""
    return f"{round(value)}{_UNITS.get(metric, '')}"


@router.get(
    "/devices/{address}/body_metrics", response_model=BoomResponse[BodyMetricsData]
)
async def get_body_metrics(
    address: str,
    metric: str = Query(
        ..., description="指标类型:heartRate/restingHeartRate/bloodOxygen/hrv"
    ),
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
    session: Session = Depends(get_session),
) -> BoomResponse[BodyMetricsData]:
    """7.1 获取身体指标数据.

    ``trendData`` is the per-day series over ``[startDate, endDate]``:
    ``restingHeartRate`` / ``hrv`` / ``bloodOxygen`` read a per-day
    ``DailySummary`` value (``bloodOxygen`` ÷10 from the device-raw ×10 SpO2);
    ``heartRate`` is the per-day mean of ``MinuteMetric.heart_rate`` (no
    per-day field exists). ``currentData`` is the latest available value for the
    metric (formatted with its unit); ``analysisText`` is the latest day's
    ``boom_go_text``.

    ``normalRange`` and ``updateTime`` have no source in the spec, model, or
    calc docs, so they are left empty (pending). A missing device or an unknown
    ``metric`` yields an empty payload.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=BodyMetricsData())

    start = _parse_date(startDate)
    end = _parse_date(endDate)
    if end < start:
        start, end = end, start

    if metric == "heartRate":
        trend = _heart_rate_trend(
            session, device.id if device.id is not None else -1, start, end
        )
        current = _latest_heart_rate(
            session, device.id if device.id is not None else -1
        )
    elif metric in _SUMMARY_METRICS:
        field, divisor = _SUMMARY_METRICS[metric]
        trend = _summary_values(
            session,
            device.id if device.id is not None else -1,
            start,
            end,
            field,
            divisor,
        )
        current = _latest_summary_value(
            session, device.id if device.id is not None else -1, field, divisor
        )
    else:
        return BoomResponse(code=0, data=BodyMetricsData())

    return BoomResponse(
        code=0,
        data=BodyMetricsData(
            currentData=_format_current(current, metric),
            trendData=trend,
            analysisText=_analysis_text(
                session, device.id if device.id is not None else -1
            ),
        ),
    )

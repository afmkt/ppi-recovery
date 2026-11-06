"""7.x 身体指标页面 — GET /devices/{address}/body_metrics."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlmodel import Session, col, select

from newboom.boom.schemas import BoomResponse, DatePoint, StaleFields
from newboom.boom.staleness import locf, mark_stale
from newboom.config import STALE_WINDOW_DAYS
from newboom.database import get_session
from newboom.model import DailySummary, Device, MinuteMetric

router = APIRouter(tags=["身体指标"])


class BodyMetricsData(StaleFields):
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
    """Per-day value from a ``DailySummary`` float field (LOCF-filled, marked
    ``carried``), divided by ``divisor`` and rounded. Fetches from
    ``start - STALE_WINDOW_DAYS`` so :func:`locf` can seed a leading carry, then
    clips back to ``[start, end]``; a day with a null value is carried from the
    last real one across a gap <= the window, else omitted."""
    column = col(getattr(DailySummary, field))
    rows = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device_id)
        .where(DailySummary.summary_date >= start - timedelta(days=STALE_WINDOW_DAYS))
        .where(DailySummary.summary_date <= end)
        .where(column.isnot(None))
        .order_by(col(DailySummary.summary_date).asc())
    ).all()
    real = [
        DatePoint(
            date=r.summary_date.isoformat(), value=round(getattr(r, field) / divisor)
        )
        for r in rows
    ]
    return locf(real, start, end)


def _heart_rate_trend(
    session: Session, device_id: int, start: date, end: date
) -> list[DatePoint]:
    """Per-day mean heart rate (``MinuteMetric.heart_rate`` over each
    ``night_date``, LOCF-filled, marked ``carried``). Fetches minutes from
    ``start - STALE_WINDOW_DAYS`` so :func:`locf` can seed a leading carry, then
    clips back to ``[start, end]``; a day with no minutes is carried from the
    last real mean across a gap <= the window, else omitted."""
    rows = session.exec(
        select(MinuteMetric)
        .where(MinuteMetric.device_id == device_id)
        .where(MinuteMetric.night_date >= start - timedelta(days=STALE_WINDOW_DAYS))
        .where(MinuteMetric.night_date <= end)
        .order_by(col(MinuteMetric.timestamp).asc())
    ).all()
    by_day: dict[date, list[float]] = {}
    for m in rows:
        if m.heart_rate is None:
            continue
        by_day.setdefault(m.night_date, []).append(m.heart_rate)
    real = [
        DatePoint(date=d.isoformat(), value=round(sum(by_day[d]) / len(by_day[d])))
        for d in sorted(by_day)
    ]
    return locf(real, start, end)


def _latest_summary_value(
    session: Session, device_id: int, field: str, divisor: float
) -> tuple[float | None, date | None]:
    """The most recent day's value from a ``DailySummary`` field, or
    ``(None, None)``. Returns the value *and* the ``summary_date`` it came from
    (the ``asOf`` source for the staleness marker)."""
    row = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device_id)
        .where(col(getattr(DailySummary, field)).isnot(None))
        .order_by(col(DailySummary.summary_date).desc())
    ).first()
    if row is None:
        return None, None
    return getattr(row, field) / divisor, row.summary_date


def _latest_heart_rate(
    session: Session, device_id: int
) -> tuple[float | None, date | None]:
    """The most recent ``MinuteMetric`` heart rate *and* the date it came from,
    or ``(None, None)``. The date is the ``asOf`` source for the marker."""
    row = session.exec(
        select(MinuteMetric)
        .where(MinuteMetric.device_id == device_id)
        .order_by(col(MinuteMetric.timestamp).desc())
    ).first()
    if row is None or row.heart_rate is None:
        return None, None
    return row.heart_rate, row.timestamp.date()


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
    ``boom_go_text``. ``trendData`` is LOCF-filled (a missing day carried from
    the last real value across a gap <= ``STALE_WINDOW_DAYS``, marked
    ``carried``). ``asOf`` / ``stale`` mark when the ``currentData`` came from
    and whether it is older than the staleness window.

    ``normalRange`` and ``updateTime`` have no source in the spec, model, or
    calc docs, so they are left empty (pending). A missing device or an unknown
    ``metric`` yields an empty payload with ``asOf=None`` / ``stale=True``.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=BodyMetricsData(asOf=None, stale=True))

    start = _parse_date(startDate)
    end = _parse_date(endDate)
    if end < start:
        start, end = end, start

    if metric == "heartRate":
        trend = _heart_rate_trend(
            session, device.id if device.id is not None else -1, start, end
        )
        current, source = _latest_heart_rate(
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
        current, source = _latest_summary_value(
            session, device.id if device.id is not None else -1, field, divisor
        )
    else:
        return BoomResponse(code=0, data=BodyMetricsData(asOf=None, stale=True))

    as_of, stale = mark_stale(source, device=device)
    return BoomResponse(
        code=0,
        data=BodyMetricsData(
            currentData=_format_current(current, metric),
            trendData=trend,
            analysisText=_analysis_text(
                session, device.id if device.id is not None else -1
            ),
            asOf=as_of,
            stale=stale,
        ),
    )

"""3.x 睡眠页面 — GET /devices/{address}/sleep_*."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

import numpy as np
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from newboom.boom.schemas import BoomResponse, DatePoint, TimePoint
from newboom.config import NIGHT_START_HOUR
from newboom.database import get_session
from newboom.model import PPI, DailySummary, Device, MinuteMetric, Sleep, WindowMetric

router = APIRouter(tags=["睡眠"])


class SleepStatusData(BaseModel):
    totalSleep: int = Field(default=0, description="总睡眠值 0-100")
    sleepProgress: int = Field(default=0, description="睡眠进度百分比 0-100")
    sleepDuration: str = Field(default="", description="睡眠时长，如 8小时10分钟")
    sleepQuality: int = Field(default=0, description="睡眠质量百分比 0-100")
    suggestedSleepDuration: str = Field(default="", description="建议睡眠时长，如 9:00")
    recoverySleepPercentage: int = Field(default=0, description="恢复性睡眠当日占比")
    recoverySleepDuration: str = Field(
        default="", description="恢复性睡眠总时长，如 4小时50分"
    )
    avgRecoverySleepPercentage: str = Field(
        default="", description="恢复性睡眠平均占比，如 52%"
    )
    avgRecoverySleepDuration: str = Field(
        default="", description="恢复性睡眠平均时长，如 4小时30分"
    )


class TimeOfDayPoint(BaseModel):
    """Date -> ``HH:mm`` (bedtime / waketime series)."""

    date: str = Field(..., description="日期 YYYY-MM-DD")
    value: str = Field(..., description="时间 HH:mm")


class SleepModeData(BaseModel):
    bedtimes: list[TimeOfDayPoint] = Field(
        default_factory=list, description="每日入睡时间"
    )
    wakeTimes: list[TimeOfDayPoint] = Field(
        default_factory=list, description="每日醒来时间"
    )
    avgBedtime: str = Field(default="", description="平均入睡时间 HH:mm")
    avgWakeTime: str = Field(default="", description="平均醒来时间 HH:mm")


# --- helpers ---------------------------------------------------------------


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _sleep_duration_hours(rec: Sleep) -> float:
    """Total sleep for a night (light + deep + other) in hours."""
    return (
        rec.light_sleep_period + rec.deep_sleep_period + rec.other_sleep_period
    ) / 3600.0


def _hours_minutes(total_minutes: float, unit: str) -> str:
    """``X小时Y<unit>`` from a minute count (total rounded; ``Y`` < 60)."""
    whole = round(total_minutes)
    hours, minutes = divmod(whole, 60)
    return f"{hours}小时{minutes}{unit}"


def _duration_hhmm(hours: float) -> str:
    """Sleep duration as ``H:MM`` (e.g. 9.0h -> ``9:00``, 9.5h -> ``9:30``)."""
    total_minutes = round(hours * 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}:{minutes:02d}"


def _hhmm_from_hours(hours: float) -> str:
    """``HH:mm`` clock time from hours-from-midnight (wraps at 24h)."""
    total_minutes = round(hours * 60) % (24 * 60)
    h, m = divmod(total_minutes, 60)
    return f"{h:02d}:{m:02d}"


def _circular_mean_hhmm(hours: list[float]) -> str:
    """Circular mean of times-of-day as ``HH:mm``.

    A time-of-day average must treat 24h as a circle: an arithmetic mean of
    the hour values is pulled across midnight by a late reading (e.g. a 23:50
    bedtime would drag the mean to ~04:00 instead of ~01:00). ``hours`` are
    hours-from-midnight; ``[]`` yields ``""``.
    """
    if not hours:
        return ""
    scale = 2 * math.pi / 24.0
    sx = sum(math.cos(scale * h) for h in hours)
    sy = sum(math.sin(scale * h) for h in hours)
    mean = math.atan2(sy, sx) / scale
    return _hhmm_from_hours(mean % 24.0)


def _format_pct(value: float) -> str:
    return f"{round(value)}%"


def _night_sleep_records(
    session: Session, device_id: int, start: date, end: date
) -> dict[date, Sleep]:
    """Latest ``Sleep`` record per night keyed to its ``night_date`` in
    ``[start, end]``.

    ``Sleep.night_date`` is a property (``night_key`` of its timestamp), not a
    column, so the night window ``[start 08:00, end+1 08:00)`` is matched on the
    ``time`` column and re-keyed in Python; the most recent record per night
    (largest ``time``) wins when several were uploaded for one night.
    """
    start_dt = datetime.combine(start, time(NIGHT_START_HOUR))
    end_dt = datetime.combine(end, time(NIGHT_START_HOUR)) + timedelta(days=1)
    rows = session.exec(
        select(Sleep)
        .where(Sleep.device_id == device_id)
        .where(col(Sleep.time) >= start_dt)
        .where(col(Sleep.time) < end_dt)
        .order_by(col(Sleep.time).asc())
    ).all()
    by_night: dict[date, Sleep] = {}
    for r in rows:
        night = r.night_date
        if start <= night <= end:
            by_night[night] = r
    return by_night


def _sleep_window(
    session: Session, device_id: int, night: date
) -> tuple[datetime, datetime] | None:
    """``[bedtime, waketime]`` for a night from its ``Sleep`` record.

    ``Sleep.time`` is the settlement moment; bedtime = ``time - sleep_onset_time``
    seconds, waketime = ``time - awake_time`` seconds (boom §3.2; the example
    night settles at 07:30 with onset 25200s / awake 1800s -> 00:30 -> 07:00).
    ``None`` when the night has no ``Sleep`` record.
    """
    night_start = datetime(night.year, night.month, night.day, NIGHT_START_HOUR)
    night_end = night_start + timedelta(days=1)
    rec = session.exec(
        select(Sleep)
        .where(Sleep.device_id == device_id)
        .where(col(Sleep.time) >= night_start)
        .where(col(Sleep.time) < night_end)
        .order_by(col(Sleep.time).desc())
    ).first()
    if rec is None:
        return None
    bed = rec.time - timedelta(seconds=rec.sleep_onset_time)
    wake = rec.time - timedelta(seconds=rec.awake_time)
    return bed, wake


def _sleep_metric_points(
    session: Session, device_id: int, night: date, metric: str
) -> list[TimePoint]:
    """Intraday ``metric`` points over a night's sleep, as ``HH:mm`` values.

    * ``heartRate`` -> 1-min heart rate (``MinuteMetric.heart_rate``) bounded to
      the ``[bedtime, waketime]`` window.
    * ``hrv`` -> sleep-window RMSSD (``WindowMetric.rmssd_ms`` with
      ``is_sleep``) of the night.
    * ``oxygen`` -> SpO2 (``PPI.spo2``) bounded to the window; ``spo2`` is the
      device-raw value (×10, ``950`` = 95.0%), divided by 10 to a percentage, and
      ``spo2 == 0`` (not reported) is skipped.

    ``heartRate`` / ``oxygen`` need the sleep window, so a missing ``Sleep``
    record yields ``[]``; an unknown metric yields ``[]``.
    """
    if metric == "hrv":
        wrows: Sequence[WindowMetric] = session.exec(
            select(WindowMetric)
            .where(WindowMetric.device_id == device_id)
            .where(WindowMetric.night_date == night)
            .where(col(WindowMetric.rmssd_ms).isnot(None))
            .where(col(WindowMetric.is_sleep).is_(True))
            .order_by(col(WindowMetric.window_start).asc())
        ).all()

        return [
            TimePoint(
                time=w.window_start.strftime("%H:%M"),
                value=round(w.rmssd_ms, 1) if w.rmssd_ms is not None else 0,
            )
            for w in wrows
        ]

    window = _sleep_window(session, device_id, night)
    if window is None:
        return []
    bed, wake = window

    if metric == "heartRate":
        mrows = session.exec(
            select(MinuteMetric)
            .where(MinuteMetric.device_id == device_id)
            .where(MinuteMetric.night_date == night)
            .order_by(col(MinuteMetric.timestamp).asc())
        ).all()
        return [
            TimePoint(time=m.timestamp.strftime("%H:%M"), value=round(m.heart_rate))
            for m in mrows
            if bed <= m.timestamp < wake
        ]

    if metric == "oxygen":
        rows = session.exec(
            select(PPI)
            .where(PPI.device_id == device_id)
            .where(col(PPI.spo2) > 0)
            .order_by(col(PPI.time).asc())
        ).all()
        return [
            TimePoint(time=p.time.strftime("%H:%M"), value=round(p.spo2 / 10))
            for p in rows
            if bed <= p.time < wake
        ]

    return []


def _sleep_trend(
    session: Session, device_id: int, start: date, end: date
) -> list[DatePoint]:
    """Per-day sleep duration in hours (``DatePoint.value``); a night with no
    ``Sleep`` record in the range is omitted."""
    recs = _night_sleep_records(session, device_id, start, end)
    return [
        DatePoint(
            date=night.isoformat(),
            value=round(_sleep_duration_hours(recs[night]), 1),
        )
        for night in sorted(recs)
    ]


# --- endpoints -------------------------------------------------------------


@router.get(
    "/devices/{address}/sleep_status", response_model=BoomResponse[SleepStatusData]
)
async def get_sleep_status(
    address: str,
    date: str | None = Query(None, description="查询日期 YYYY-MM-DD，不传默认当天"),
    session: Session = Depends(get_session),
) -> BoomResponse[SleepStatusData]:
    """3.1 获取睡眠页面基础数据.

    Reads the precomputed ``DailySummary`` for the day (latest when ``date`` is
    omitted). ``totalSleep`` / ``sleepProgress`` / ``sleepQuality`` are all
    ``healthStatus.sleep`` (``DailySummary.sleep_quality``; spec 备注).
    ``sleepDuration`` is the target night's total sleep (``Sleep`` record);
    ``suggestedSleepDuration`` is ``max(8h, avg + std)`` over the trailing 7-day
    sleep durations; the ``recoverySleep*`` / ``avgRecoverySleep*`` fields come
    from the day's and 7-day-window ``recovery_sleep_percentage`` /
    ``recovery_sleep_duration_min``. A missing device / day yields a zeroed
    payload (``BoomResponse`` carries no error field).
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=SleepStatusData())

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
        return BoomResponse(code=0, data=SleepStatusData())

    day = summary.summary_date
    window_start = day - timedelta(days=6)

    # --- per-night sleep durations (trailing 7-day window) ---
    recs = _night_sleep_records(
        session, device.id if device.id is not None else 0, window_start, day
    )
    durations = [_sleep_duration_hours(r) for r in recs.values()]
    target_rec = recs.get(day)

    sleep_duration = ""
    if target_rec is not None:
        sleep_duration = _hours_minutes(_sleep_duration_hours(target_rec) * 60, "分钟")

    suggested_duration = ""
    if durations:
        avg = float(np.mean(durations))
        std = float(np.std(durations)) if len(durations) > 1 else 0.0
        suggested_duration = _duration_hhmm(max(8.0, avg + std))

    # --- recovery sleep: today's value + 7-day averages ---
    recovery_pct = summary.recovery_sleep_percentage or 0
    recovery_duration = ""
    if summary.recovery_sleep_duration_min is not None:
        recovery_duration = _hours_minutes(summary.recovery_sleep_duration_min, "分")

    summaries = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device.id)
        .where(DailySummary.summary_date >= window_start)
        .where(DailySummary.summary_date <= day)
    ).all()
    pcts = [
        s.recovery_sleep_percentage
        for s in summaries
        if s.recovery_sleep_percentage is not None
    ]
    rec_mins = [
        s.recovery_sleep_duration_min
        for s in summaries
        if s.recovery_sleep_duration_min is not None
    ]
    avg_recovery_pct = _format_pct(float(np.mean(pcts))) if pcts else ""
    avg_recovery_dur = (
        _hours_minutes(float(np.mean(rec_mins)), "分") if rec_mins else ""
    )

    total_sleep = summary.sleep_quality or 0
    return BoomResponse(
        code=0,
        data=SleepStatusData(
            totalSleep=total_sleep,
            sleepProgress=total_sleep,
            sleepQuality=total_sleep,
            sleepDuration=sleep_duration,
            suggestedSleepDuration=suggested_duration,
            recoverySleepPercentage=recovery_pct,
            recoverySleepDuration=recovery_duration,
            avgRecoverySleepPercentage=avg_recovery_pct,
            avgRecoverySleepDuration=avg_recovery_dur,
        ),
    )


@router.get(
    "/devices/{address}/sleep_metric", response_model=BoomResponse[list[TimePoint]]
)
async def get_sleep_metric(
    address: str,
    date: str = Query(..., description="查询日期 YYYY-MM-DD"),
    metric: str = Query(..., description="指标类型：heartRate/hrv/oxygen"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[TimePoint]]:
    """3.2 获取睡眠期间数据.

    Returns the requested metric (``heartRate`` / ``hrv`` / ``oxygen``) over the
    night of ``date`` as intraday ``HH:mm`` points (see :func:`_sleep_metric_points`).
    A missing device yields ``[]``.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=[])
    night = _parse_date(date)
    return BoomResponse(
        code=0,
        data=_sleep_metric_points(
            session, device.id if device.id is not None else 0, night, metric
        ),
    )


@router.get(
    "/devices/{address}/sleep_trend", response_model=BoomResponse[list[DatePoint]]
)
async def get_sleep_trend(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[DatePoint]]:
    """3.3 获取睡眠时长趋势数据（value 为小时数）.

    Per-day total sleep in hours over ``[startDate, endDate]``; a night with no
    ``Sleep`` record is omitted. A missing device yields ``[]``.
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
        data=_sleep_trend(
            session, device.id if device.id is not None else 0, start, end
        ),
    )


@router.get("/devices/{address}/sleep_mode", response_model=BoomResponse[SleepModeData])
async def get_sleep_mode(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
    session: Session = Depends(get_session),
) -> BoomResponse[SleepModeData]:
    """3.4 获取睡眠模式数据.

    Per-night bedtime / waketime (``HH:mm``) from each night's ``Sleep`` record
    (bedtime = ``time - sleep_onset_time``, waketime = ``time - awake_time``);
    ``avgBedtime`` / ``avgWakeTime`` are the circular mean over the range (see
    :func:`_circular_mean_hhmm`). Nights without a ``Sleep`` record are omitted.
    A missing device yields an empty ``SleepModeData``.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=SleepModeData())

    start = _parse_date(startDate)
    end = _parse_date(endDate)
    if end < start:
        start, end = end, start

    recs = _night_sleep_records(
        session, device.id if device.id is not None else 0, start, end
    )
    bedtimes: list[TimeOfDayPoint] = []
    wake_times: list[TimeOfDayPoint] = []
    bed_hours: list[float] = []
    wake_hours: list[float] = []
    for night in sorted(recs):
        rec = recs[night]
        bed = rec.time - timedelta(seconds=rec.sleep_onset_time)
        wake = rec.time - timedelta(seconds=rec.awake_time)
        bedtimes.append(
            TimeOfDayPoint(date=night.isoformat(), value=bed.strftime("%H:%M"))
        )
        wake_times.append(
            TimeOfDayPoint(date=night.isoformat(), value=wake.strftime("%H:%M"))
        )
        bed_hours.append(bed.hour + bed.minute / 60.0)
        wake_hours.append(wake.hour + wake.minute / 60.0)

    return BoomResponse(
        code=0,
        data=SleepModeData(
            bedtimes=bedtimes,
            wakeTimes=wake_times,
            avgBedtime=_circular_mean_hhmm(bed_hours),
            avgWakeTime=_circular_mean_hhmm(wake_hours),
        ),
    )

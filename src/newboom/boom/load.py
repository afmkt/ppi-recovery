"""5.x 负荷页面 — GET /devices/{address}/load_* / sport_load / sleep_pressure."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from newboom.boom.schemas import BoomResponse, DatePoint, TimePoint
from newboom.config import (
    DEFAULT_HR_MAX,
    DEFAULT_HR_REST,
    LOAD_THRESHOLD_FRAC,
    NIGHT_START_HOUR,
)
from newboom.database import get_session
from newboom.model import DailySummary, Device, MinuteMetric, Sleep

router = APIRouter(tags=["负荷"])


class LoadZone(BaseModel):
    percentage: int = Field(default=0, description="占比百分比")
    hours: int = Field(default=0, description="时长（小时）")


class LoadZones(BaseModel):
    overload: LoadZone = Field(
        default_factory=lambda d: LoadZone(**d), description="负荷过载区间"
    )
    warning: LoadZone = Field(
        default_factory=lambda d: LoadZone(**d), description="注意负荷区间"
    )
    normal: LoadZone = Field(
        default_factory=lambda d: LoadZone(**d), description="状态正常区间"
    )
    excellent: LoadZone = Field(
        default_factory=lambda d: LoadZone(**d), description="状态优秀区间"
    )


class HeartRateZoneItems(BaseModel):
    zone0: int = Field(default=0, description="Zone0 占比 %")
    zone1: int = Field(default=0, description="Zone1 占比 %")
    zone2: int = Field(default=0, description="Zone2 占比 %")
    zone3: int = Field(default=0, description="Zone3 占比 %")
    zone4: int = Field(default=0, description="Zone4 占比 %")
    zone5: int = Field(default=0, description="Zone5 占比 %")


class LoadStatusData(BaseModel):
    totalLoad: int = Field(default=0, description="总负荷值 0-100")
    loadProgress: int = Field(default=0, description="负荷进度百分比 0-100")
    avgLoad: int = Field(default=0, description="平均负荷")
    restingHeartRate: int = Field(default=0, description="静息心率 bpm")
    steps: int = Field(default=0, description="步数")
    calories: int = Field(default=0, description="卡路里")
    realtimeLoadData: list[TimePoint] = Field(
        default_factory=list, description="当日实时负荷数据"
    )
    loadZones: LoadZones = Field(default_factory=LoadZones, description="负荷区间状态")
    heartRateZoneData: list[TimePoint] = Field(
        default_factory=list, description="心率区间趋势数据"
    )
    heartRateZoneItems: HeartRateZoneItems = Field(
        default_factory=lambda d: HeartRateZoneItems(**d), description="心率区间分布"
    )


# --- helpers ---------------------------------------------------------------


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _hr_rest(summary: DailySummary) -> float:
    """Resting HR for the load math: today's value, else the 7-day baseline,
    else the fixed default (spec §7 "取过去7天静息心率基线")."""
    if summary.rest_heart_rate_val is not None:
        return float(summary.rest_heart_rate_val)
    if summary.rest_heart_rate_avg is not None:
        return float(summary.rest_heart_rate_avg)
    return DEFAULT_HR_REST


def _baseline_hr(summary: DailySummary) -> float:
    """Non-exercise/non-sleep HR baseline (spec loadZones "状态优秀" baseline):
    the 7-day rolling mean, else today's value, else the resting HR."""
    if summary.non_sport_sleep_hr_avg is not None:
        return float(summary.non_sport_sleep_hr_avg)
    if summary.non_sport_sleep_hr_val is not None:
        return float(summary.non_sport_sleep_hr_val)
    return _hr_rest(summary)


def _sleep_window(
    session: Session, device_id: int, day: date
) -> tuple[datetime, datetime] | None:
    """``[bedtime, waketime]`` for the night of ``day`` (mirrors sleep §3.2);
    ``None`` when there is no ``Sleep`` record."""
    night_start = datetime(day.year, day.month, day.day, NIGHT_START_HOUR)
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
    return (
        rec.time - timedelta(seconds=rec.sleep_onset_time),
        rec.time - timedelta(seconds=rec.awake_time),
    )


def _minutes(session: Session, device_id: int, day: date) -> list[MinuteMetric]:
    """That day's 1-min metrics (``night_date`` == ``day``), chronological."""
    return list(
        session.exec(
            select(MinuteMetric)
            .where(MinuteMetric.device_id == device_id)
            .where(MinuteMetric.night_date == day)
            .order_by(col(MinuteMetric.timestamp).asc())
        ).all()
    )


def _realtime_load(minutes: list[MinuteMetric]) -> list[TimePoint]:
    """Per-minute cumulative total load ``totalLoad_(d,t)`` (calc §7: the
    "实时负荷曲线" — load accumulates per minute, growing over time)."""
    return [
        TimePoint(time=m.timestamp.strftime("%H:%M"), value=round(m.total_load))
        for m in minutes
    ]


def _hourly_hr(minutes: list[MinuteMetric]) -> list[TimePoint]:
    """Per-hour average heart-rate trend (``HH:00`` -> mean HR)."""
    by_hour: dict[int, list[float]] = {}
    for m in minutes:
        if m.heart_rate is None:
            continue
        by_hour.setdefault(m.timestamp.hour, []).append(m.heart_rate)
    return [
        TimePoint(time=f"{h:02d}:00", value=round(sum(by_hour[h]) / len(by_hour[h])))
        for h in sorted(by_hour)
    ]


def _hr_zone_items(minutes: list[MinuteMetric]) -> HeartRateZoneItems:
    """Per-zone share of minutes (``heart_rate_zone`` 0-5), each as a percent."""
    counts = [0] * 6
    total = 0
    for m in minutes:
        zone = m.heart_rate_zone or 0
        if 0 <= zone <= 5:
            counts[zone] += 1
        total += 1
    if total == 0:
        return HeartRateZoneItems()
    pct = [int(round(100 * c / total)) for c in counts]
    return HeartRateZoneItems(
        zone0=pct[0],
        zone1=pct[1],
        zone2=pct[2],
        zone3=pct[3],
        zone4=pct[4],
        zone5=pct[5],
    )


def _load_zones(minutes: list[MinuteMetric], summary: DailySummary) -> LoadZones:
    """Per-minute load-zone classification (spec loadZones).

    Precedence: overload (``HR > 45%HRR + RHR``) > warning (``HR > baseline+std``)
    > normal (``baseline <= HR <= baseline+std``) > excellent (``HR < baseline``).
    ``baseline`` / ``std`` are the non-exercise/non-sleep HR mean/std; the
    overload threshold uses the day's ``hr_max`` / resting HR.
    """
    if not minutes:
        return LoadZones()
    baseline = _baseline_hr(summary)
    std = (
        float(summary.non_sport_sleep_hr_std)
        if summary.non_sport_sleep_hr_std is not None
        else 0.0
    )
    hr_rest = _hr_rest(summary)
    hr_max = float(summary.hr_max or DEFAULT_HR_MAX)
    threshold = LOAD_THRESHOLD_FRAC * (hr_max - hr_rest) + hr_rest
    upper = baseline + std

    counts = {"overload": 0, "warning": 0, "normal": 0, "excellent": 0}
    total = 0
    for m in minutes:
        hr = m.heart_rate
        if hr is None:
            continue
        total += 1
        if hr > threshold:
            counts["overload"] += 1
        elif hr > upper:
            counts["warning"] += 1
        elif hr < baseline:
            counts["excellent"] += 1
        else:
            counts["normal"] += 1

    def zone(c: int) -> LoadZone:
        if total == 0:
            return LoadZone()
        return LoadZone(
            percentage=int(round(100 * c / total)), hours=int(round(c / 60))
        )

    return LoadZones(
        overload=zone(counts["overload"]),
        warning=zone(counts["warning"]),
        normal=zone(counts["normal"]),
        excellent=zone(counts["excellent"]),
    )


def _load_status_data(
    session: Session, device: Device, day: date
) -> LoadStatusData | None:
    """Assembles 5.1 from the day's ``DailySummary`` (scalars) and
    ``MinuteMetric`` (per-minute curves / zones). ``None`` when the day has no
    summary -> the caller returns a zeroed payload."""
    summary = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device.id)
        .where(DailySummary.summary_date == day)
    ).first()
    if summary is None:
        return None

    total_load = summary.load_score or 0
    rhr = (
        round(summary.rest_heart_rate_val)
        if summary.rest_heart_rate_val is not None
        else 0
    )

    minutes = _minutes(session, device.id if device.id is not None else -1, day)
    window = _sleep_window(session, device.id if device.id is not None else -1, day)

    steps = sum((m.steps or 0) for m in minutes)
    calories = round(sum((m.calories or 0.0) for m in minutes))

    # avgLoad: the day's mean HR, excluding the sleep phase (spec "除去睡眠阶段").
    hr_values = [
        m.heart_rate
        for m in minutes
        if m.heart_rate is not None
        and (window is None or not (window[0] <= m.timestamp < window[1]))
    ]
    avg_load = round(sum(hr_values) / len(hr_values)) if hr_values else 0

    return LoadStatusData(
        totalLoad=total_load,
        loadProgress=total_load,
        avgLoad=avg_load,
        restingHeartRate=rhr,
        steps=steps,
        calories=calories,
        realtimeLoadData=_realtime_load(minutes),
        loadZones=_load_zones(minutes, summary),
        heartRateZoneData=_hourly_hr(minutes),
        heartRateZoneItems=_hr_zone_items(minutes),
    )


@router.get(
    "/devices/{address}/load_status", response_model=BoomResponse[LoadStatusData]
)
async def get_load_status(
    address: str,
    date: str | None = Query(None, description="查询日期 YYYY-MM-DD，不传默认当天"),
    session: Session = Depends(get_session),
) -> BoomResponse[LoadStatusData]:
    """5.1 获取负荷页面基础数据.

    Reads the day's ``DailySummary`` for the scalars: ``totalLoad`` /
    ``loadProgress`` = ``load_score``; ``restingHeartRate`` = ``rest_heart_rate_val``.
    ``steps`` / ``calories`` sum the day's ``MinuteMetric``. The per-minute
    curves and zone breakdowns come from ``MinuteMetric``: ``realtimeLoadData``
    is the cumulative total load, ``heartRateZoneData`` the per-hour HR trend,
    ``heartRateZoneItems`` the zone distribution, ``loadZones`` the per-minute
    classification. ``avgLoad`` is the day's mean HR excluding the sleep phase.
    A missing device / day yields a zeroed payload.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=LoadStatusData())

    if date:
        day = _parse_date(date)
    else:
        latest = session.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device.id)
            .order_by(col(DailySummary.summary_date).desc())
        ).first()
        if latest is None:
            return BoomResponse(code=0, data=LoadStatusData())
        day = latest.summary_date

    data = _load_status_data(session, device, day)
    if data is None:
        return BoomResponse(code=0, data=LoadStatusData())
    return BoomResponse(code=0, data=data)


def _load_trend(
    session: Session, device_id: int, start: date, end: date
) -> list[DatePoint]:
    """Per-day total load (``DailySummary.total_load``); days without a
    finalized value are omitted."""
    rows = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device_id)
        .where(DailySummary.summary_date >= start)
        .where(DailySummary.summary_date <= end)
        .where(col(DailySummary.total_load).isnot(None))
        .order_by(col(DailySummary.summary_date).asc())
    ).all()
    return [
        DatePoint(
            date=r.summary_date.isoformat(),
            value=round(r.total_load) if r.total_load is not None else -1,
        )
        for r in rows
    ]


def _sport_trend(
    session: Session, device_id: int, start: date, end: date
) -> list[DatePoint]:
    """Per-day sport load (``DailySummary.total_tss``); days without a
    finalized value are omitted."""
    rows = session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device_id)
        .where(DailySummary.summary_date >= start)
        .where(DailySummary.summary_date <= end)
        .where(col(DailySummary.total_tss).isnot(None))
        .order_by(col(DailySummary.summary_date).asc())
    ).all()
    return [
        DatePoint(
            date=r.summary_date.isoformat(),
            value=round(r.total_tss) if r.total_tss is not None else -1,
        )
        for r in rows
    ]


@router.get(
    "/devices/{address}/load_trend", response_model=BoomResponse[list[DatePoint]]
)
async def get_load_trend(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[DatePoint]]:
    """5.2 获取负荷趋势数据（value = 当日 totalLoad）.

    Per-day total load over ``[startDate, endDate]``; a day without a finalized
    ``total_load`` is omitted. A missing device yields ``[]``.
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
        data=_load_trend(
            session, device.id if device.id is not None else -1, start, end
        ),
    )


@router.get(
    "/devices/{address}/sport_load", response_model=BoomResponse[list[DatePoint]]
)
async def get_sport_load(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[DatePoint]]:
    """5.3 获取运动负荷数据（value = 运动 tss）.

    Per-day sport load (``total_tss``) over ``[startDate, endDate]``; a day
    without a finalized value is omitted. A missing device yields ``[]``.
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
        data=_sport_trend(
            session, device.id if device.id is not None else -1, start, end
        ),
    )


@router.get(
    "/devices/{address}/sleep_pressure", response_model=BoomResponse[list[DatePoint]]
)
async def get_sleep_pressure(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[DatePoint]]:
    """5.4 获取睡眠压力数据.

    PENDING: the spec gives a response shape and an example trend but no source
    field, formula, or precomputed metric for "睡眠压力" (it is absent from the
    model, ``boom_compute``, and both calc docs). Returns ``[]`` until a source
    is defined; a missing device yields ``[]``.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=[])
    return BoomResponse(code=0, data=[])

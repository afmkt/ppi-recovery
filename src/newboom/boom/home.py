"""1.1 获取首页训练状态 — GET /devices/{address}/go."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from newboom.boom.load import day_total_load
from newboom.boom.schemas import BoomResponse, StaleFields
from newboom.boom.staleness import mark_stale
from newboom.database import get_session
from newboom.model import DailySummary, Device, MinuteMetric

router = APIRouter(tags=["首页"])


class HealthStatus(BaseModel):
    status: int = Field(default=0, description="综合状态百分比 0-100")
    sleep: int = Field(default=0, description="睡眠状态百分比 0-100")
    load: int = Field(default=0, description="当日累计总负荷（= 负荷页 totalLoad）")


class HealthCardValues(BaseModel):
    heartRate: int = Field(default=0, description="当前心率 bpm")
    restingHeartRate: int = Field(default=0, description="静息心率 bpm")
    oxygen: int = Field(default=0, description="血氧饱和度 %")
    hrv: int = Field(default=0, description="心率变异性 ms(上一次设备传输）")


class GoDetails(BaseModel):
    supercompensationTime: str = Field(default="", description="超量恢复时间段")
    suggestion: str = Field(default="", description="训练建议（可选）")
    duration: str = Field(default="", description="建议时长（可选）")
    target: str = Field(default="", description="训练目标（可选）")


class GoData(StaleFields):
    healthStatus: HealthStatus = Field(default_factory=lambda d: HealthStatus(**d))
    boomGoText: str = Field(default="", description="BOOM GO 模块文案")
    energyPercentage: int = Field(default=0, description="储能环形图百分比 0-100")
    healthCardValues: HealthCardValues = Field(
        default_factory=lambda d: HealthCardValues(**d)
    )
    details: GoDetails = Field(default_factory=lambda d: GoDetails(**d))


# --- helpers ---------------------------------------------------------------


def _latest_summary(session: Session, device_id: int) -> DailySummary | None:
    """The device's most recent ``DailySummary``; ``None`` when the device has
    no daily summary yet. §1.1 has no ``date`` parameter, so the home page
    tracks the latest day — the same rule the per-page status/sleep/energy/load
    endpoints apply when their optional ``date`` is omitted."""
    return session.exec(
        select(DailySummary)
        .where(DailySummary.device_id == device_id)
        .order_by(col(DailySummary.summary_date).desc())
    ).first()


def _latest_heart_rate(session: Session, device_id: int) -> int:
    """The most recent 1-min heart rate as the 'current' HR card value; ``0``
    when the device has no ``MinuteMetric`` yet (mirrors body_metrics §7.1)."""
    row = session.exec(
        select(MinuteMetric)
        .where(MinuteMetric.device_id == device_id)
        .order_by(col(MinuteMetric.timestamp).desc())
    ).first()
    return round(row.heart_rate) if row and row.heart_rate is not None else 0


def _energy_percentage(session: Session, device_id: int, summary: DailySummary) -> int:
    """Day-end remaining energy ``F_d`` as a 0-100 ring value, mirroring energy
    §4.1 ``totalEnergy``: the finalized ``energy_end_f`` when the day is closed,
    else the last valid per-minute energy for the day, else the morning ``E_d``."""
    f_d = summary.energy_end_f
    if f_d is None:
        last = session.exec(
            select(MinuteMetric)
            .where(MinuteMetric.device_id == device_id)
            .where(MinuteMetric.night_date == summary.summary_date)
            .order_by(col(MinuteMetric.timestamp).desc())
        ).first()
        f_d = last.total_energy / 100.0 if last else summary.energy_start_e
    return round(100 * max(0.0, min(1.0, f_d)))


@router.get("/devices/{address}/go", response_model=BoomResponse[GoData])
async def get_home(
    address: str,
    session: Session = Depends(get_session),
) -> BoomResponse[GoData]:
    """1.1 获取首页训练状态.

    Assembles the home dashboard from the device's most recent ``DailySummary``
    (the page has no date parameter, so it tracks the latest day — the same rule
    the per-page status/sleep/energy/load endpoints apply when ``date`` is
    omitted):

    * ``healthStatus.status`` — the overall status: ``status_current`` (the
      latest hourly-adjusted value), else the frozen ``status_score`` — the same
      source as status §2.1 ``totalStatus``.
    * ``healthStatus.sleep`` — ``sleep_quality`` (= sleep §3.1 ``totalSleep``).
    * ``healthStatus.load`` — the day's actual accumulated total load
      (= load §5.1 ``totalLoad``; the finalized day total, or the last
      minute's cumulative load for the in-progress day — *not* ``load_score``,
      a fixed 65 status default in the first-7-days temporary mode).
    * ``boomGoText`` — the day's coaching text (``boom_go_text``).
    * ``energyPercentage`` — day-end energy ``F_d`` as a 0-100 ring
      (= energy §4.1 ``totalEnergy``; :func:`_energy_percentage`).
    * ``healthCardValues`` — ``heartRate`` the most recent 1-min HR
      (``MinuteMetric``), ``restingHeartRate`` ``rest_heart_rate_val``,
      ``oxygen`` the night SpO2 mean ``spo2_night_avg`` ÷10 (device-raw ×10,
      same as body_metrics §7.1 ``bloodOxygen``), ``hrv`` the night RMSSD mean
      ``hrv_night_val``.
    * ``details`` — the supercompensation recommendation (``supercomp_time`` /
      ``training_suggestion`` / ``training_duration`` / ``training_target``).

    A missing device or a device with no ``DailySummary`` yields a zeroed
    ``GoData`` (``BoomResponse`` carries no error field) with ``asOf=None`` /
    ``stale=True`` so the client can tell "no data" from a real zero. With a
    summary present, ``asOf`` is its ``summary_date`` and ``stale`` is True when
    that day is older than ``STALE_WINDOW_DAYS`` (device-local).
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        return BoomResponse(code=0, data=GoData(asOf=None, stale=True))

    device_id = device.id if device.id is not None else -1
    summary = _latest_summary(session, device_id)
    if summary is None:
        # No data at all -> zeroed values, but mark it so the UI knows it is
        # "no data" rather than a real zero.
        return BoomResponse(code=0, data=GoData(asOf=None, stale=True))

    as_of, stale = mark_stale(summary.summary_date, device=device)
    status = (
        summary.status_current
        if summary.status_current is not None
        else (summary.status_score or 0)
    )
    rhr = (
        round(summary.rest_heart_rate_val)
        if summary.rest_heart_rate_val is not None
        else 0
    )
    oxygen = (
        round(summary.spo2_night_avg / 10) if summary.spo2_night_avg is not None else 0
    )
    hrv = round(summary.hrv_night_val) if summary.hrv_night_val is not None else 0

    return BoomResponse(
        code=0,
        data=GoData(
            healthStatus=HealthStatus(
                status=status,
                sleep=summary.sleep_quality or 0,
                load=day_total_load(session, device_id, summary),
            ),
            boomGoText=summary.boom_go_text or "",
            asOf=as_of,
            stale=stale,
            energyPercentage=_energy_percentage(session, device_id, summary),
            healthCardValues=HealthCardValues(
                heartRate=_latest_heart_rate(session, device_id),
                restingHeartRate=rhr,
                oxygen=oxygen,
                hrv=hrv,
            ),
            details=GoDetails(
                supercompensationTime=summary.supercomp_time or "",
                suggestion=summary.training_suggestion or "",
                duration=summary.training_duration or "",
                target=summary.training_target or "",
            ),
        ),
    )

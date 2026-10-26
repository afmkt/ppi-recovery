from datetime import date, datetime
from enum import IntEnum, StrEnum
from typing import Optional

import numpy as np
from sqlmodel import (
    Field,
    Relationship,
    SQLModel,
    UniqueConstraint,
)
from sqlmodel.main import NaiveDatetime

from newboom.config import night_key


class Device(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    mac: str = Field(index=True, unique=True)
    created_at: NaiveDatetime = Field(default_factory=datetime.utcnow)
    tz: str | None = Field(default=None, description="Device local UTC offset, e.g. '08:00'")
    is_female: bool = Field(default=False, description="Selects the female tss load formula")
    age: int | None = Field(default=None, description="User age; HR_max = 220 - age when set")
    ppi: list["PPI"] = Relationship(back_populates="device")
    sleep: list["Sleep"] = Relationship(back_populates="device")
    window_metrics: list["WindowMetric"] = Relationship(back_populates="device")
    progress: Optional["Progress"] = Relationship(
        back_populates="device", sa_relationship_kwargs={"uselist": False}
    )
    rmssd_segments: list["RmssdSegment"] = Relationship(back_populates="device")
    daily_summary: list["DailySummary"] = Relationship(back_populates="device")
    minute_metrics: list["MinuteMetric"] = Relationship(back_populates="device")
    hourly_status: list["HourlyStatus"] = Relationship(
        back_populates="device"
    )


class ActivityState(IntEnum):
    DEEP_SLEEP = 0
    LIGHT_SLEEP = 1
    OTHER_SLEEP = 2
    RELAXED = 3
    LOW_ACTIVITY = 4
    HIGH_ACTIVITY = 5
    EXCITED = 6
    STRESSED = 7

    @property
    def is_sleep(self) -> bool:
        return self in {
            ActivityState.DEEP_SLEEP,
            ActivityState.LIGHT_SLEEP,
            ActivityState.OTHER_SLEEP,
        }


class PPI(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    time: NaiveDatetime = Field(index=True)
    hr: int = Field()
    spo2: int = Field()
    ppi: int = Field()
    ppi_clean: float | None = Field(default=None)
    fresh: bool = Field(default=True, index=True)
    activity: ActivityState = Field()
    device_id: int = Field(foreign_key="device.id")
    device: Device = Relationship(back_populates="ppi")
    __table_args__ = (UniqueConstraint("device_id", "time", name="uix_device_time"),)


class Sleep(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    timezone: str = Field()
    time: NaiveDatetime = Field(index=True)
    sleep_onset_time: int = Field()
    awake_time: int = Field()
    light_sleep_period: int = Field()
    deep_sleep_period: int = Field()
    other_sleep_period: int = Field()
    heart_rate_rest: int = Field()
    fresh: bool = Field(default=True, index=True)
    device_id: int = Field(foreign_key="device.id")
    device: Device = Relationship(back_populates="sleep")
    __table_args__ = (UniqueConstraint("device_id", "time", name="uix_device_time"),)

    @property
    def night_date(self) -> date:
        return night_key(self.time)

    @property
    def duration_hours(self) -> int:
        return int(
            (self.light_sleep_period + self.deep_sleep_period + self.other_sleep_period)
            / 3600
        )


class Task(StrEnum):
    WINDOW_METRIC = "WindowMetric"
    MINUTE_METRIC = "MinuteMetric"
    DAILY_SUMMARY = "DailySummary"


class Progress(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id")
    device: Device = Relationship(back_populates="progress")
    task: Task
    end_time: NaiveDatetime | None = Field(
        default=None, description="Tracks up to where window metrics were computed"
    )
    __table_args__ = (UniqueConstraint("device_id", "task", name="uix_device_task"),)


class WindowState(StrEnum):
    SYMPATHETIC = "sympathetic"
    PARASYMPATHETIC = "parasympathetic"
    TRANSITION = "transition"
    INVALID = "invalid"

    @staticmethod
    def from_float(value: float) -> "WindowState":
        if not np.isfinite(value):
            return WindowState.INVALID
        if value >= 2.5:
            return WindowState.SYMPATHETIC
        if value <= 1.5:
            return WindowState.PARASYMPATHETIC
        return WindowState.TRANSITION


class WindowMetric(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    device: Device = Relationship(back_populates="window_metrics")
    night_date: date = Field(
        index=True, description="The calendar date or night session identifier"
    )
    window_start: NaiveDatetime = Field(index=True)
    window_end: NaiveDatetime = Field()
    window_mid_h: float = Field(description="Hours from start of night")

    # Quality control
    valid_ratio: float = Field()

    # Time-domain HRV
    rmssd_ms: float | None = Field(default=None)
    ln_rmssd: float | None = Field(default=None)

    # Frequency-domain HRV
    lf: float | None = Field(default=None)
    hf: float | None = Field(default=None)
    lf_hf: float | None = Field(default=None)
    hf_nu: float | None = Field(default=None)

    # Ribbon state classification ('sympathetic', 'parasympathetic', 'transition', 'invalid')
    state: WindowState | None = Field(default=None)
    is_sleep: bool | None = Field(
        default=None,
        description="True when the window's majority PPI activity state is sleep",
    )

    fresh: bool = Field(default=True, index=True)
    __table_args__ = (
        UniqueConstraint("device_id", "window_start", name="uix_device_window_start"),
    )


class TrendType(StrEnum):
    RISING = "rising"
    FALLING = "falling"
    STABLE = "stable"

    @staticmethod
    def from_float(delta: float, slope: float) -> "TrendType":
        if delta > 0.05 and slope > 0.03:
            return TrendType.RISING
        if delta < -0.05 and slope < -0.03:
            return TrendType.FALLING
        return TrendType.STABLE


class RmssdSegment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    device: Device = Relationship(back_populates="rmssd_segments")

    night_date: date = Field(index=True, description="Night session date")
    segment_id: int = Field(
        description="Sequence number of the segment in the night (1, 2, 3...)"
    )

    # Timing
    start_h: float = Field(description="Start time in hours from night start")
    end_h: float = Field(description="End time in hours from night start")
    duration_min: float = Field(description="Segment duration in minutes")
    window_count: int = Field(description="Number of windows contained in this segment")

    # Trend classification
    trend_type: TrendType = Field(description="rising, falling, or stable")

    # Linear fit metrics
    lnrmssd_start: float
    lnrmssd_end: float
    lnrmssd_fitted_start: float
    lnrmssd_fitted_end: float
    lnrmssd_delta: float = Field(description="Fitted end - fitted start")
    lnrmssd_slope_per_h: float = Field(description="Slope per hour")

    # Volatility and Error
    segment_volatility: float = Field(description="Std dev of residuals")
    segment_sse: float = Field(description="Sum of squared errors")


class DailySummary(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    device: Device = Relationship(back_populates="daily_summary")

    summary_date: date = Field(
        index=True, description="Calendar date for this daily summary"
    )

    # --- Baselines (7-Day Rolling) ---

    hrv_night_avg: float | None = None
    hrv_night_std: float | None = None
    hrv_morning_avg: float | None = None
    hrv_morning_std: float | None = None
    bed_time_avg: float | None = Field(
        None, description="Average initial sleep time in hours from midnight"
    )
    rest_heart_rate_avg: float | None = None
    rest_heart_rate_std: float | None = None
    non_sport_sleep_hr_avg: float | None = None
    non_sport_sleep_hr_std: float | None = None
    hr_max: float = Field(default=190.0, description="Dynamic maximum heart rate")
    spo2_night_avg: float | None = Field(
        default=None,
        description="Night SpO2 mean from PPI.spo2 (0 = not reported, filtered out)",
    )
    spo2_night_min: float | None = Field(
        default=None,
        description="Night SpO2 min from PPI.spo2 (0 = not reported, filtered out)",
    )

    # --- Health Status Scores ---
    status_score: int | None = Field(
        None, description="Final morning status score (30–90 or 0–100)"
    )
    base_score: int | None = Field(50, description="Base score before adjustments")
    sleep_score: int | None = Field(
        50, description="Sleep duration/onset score component"
    )
    is_temporary_algorithm: bool = Field(
        default=True, description="True if using first-7-days fallback rule"
    )
    status_morning: int | None = Field(
        default=None, description="Frozen morning status score (hourly restart point)"
    )
    status_current: int | None = Field(
        default=None, description="Latest hourly-adjusted status score"
    )
    deduction_points: int = Field(
        default=0, description="Formal status deduction total (-5/-10 tiers)"
    )
    bonus_points: int = Field(
        default=0, description="Formal status bonus total (+5/+10 tiers)"
    )

    # --- Energy Store State ---
    energy_start_e: float = Field(
        default=1.0, description="Morning starting energy E_d (0 to 1)"
    )
    energy_end_f: float = Field(default=1.0, description="Day-end remaining energy F_d")
    body_state_modifier_t: float = Field(
        default=1.0, description="Body state adjustment factor T_d"
    )
    total_load: float | None = Field(
        default=None,
        description="Day's own accumulated total load (S1 + tss); filled at close-the-day finalize",
    )
    total_tss: float | None = Field(
        default=None,
        description="Day's own accumulated tss; filled at close-the-day finalize",
    )
    energy_incomplete: bool = Field(
        default=False,
        description="True when the day's F_d could not be finalized from complete data",
    )
    last_valid_energy_ts: NaiveDatetime | None = Field(
        default=None, description="Timestamp of the last valid energy point"
    )

    # --- v1.0 UI Breakdown Fields ---
    load_score: int | None = Field(
        default=None, description="Load status percentage 0-100"
    )
    sleep_quality: int | None = Field(
        default=None, description="Sleep quality percentage 0-100"
    )
    recovery_sleep_percentage: int | None = Field(default=None)
    recovery_sleep_duration_min: int | None = Field(default=None)

    # --- Training Load & Supercompensation (TSB/ATL/CTL) ---
    atl: float | None = Field(default=None, description="Short-term load")
    ctl: float | None = Field(default=None, description="Long-term load")
    tsb: float | None = Field(default=None, description="Training stress balance")

    # --- Coaching & Recommendations ---
    boom_go_text: str | None = Field(default=None)
    supercomp_time: str | None = Field(default=None)
    training_suggestion: str | None = Field(default=None)
    training_duration: str | None = Field(default=None)
    training_target: str | None = Field(default=None)

    __table_args__ = (
        UniqueConstraint("device_id", "summary_date", name="uix_device_summary_date"),
    )


class MinuteMetric(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    device: Device = Relationship(back_populates="minute_metrics")

    timestamp: NaiveDatetime = Field(index=True, description="Exact minute timestamp")
    night_date: date = Field(index=True, description="Associated night session date")

    heart_rate: float = Field(description="1-minute average heart rate")
    s1_load: float = Field(default=0.0, description="Cumulative non-movement load S1")
    tss_load: float = Field(default=0.0, description="Cumulative workout load tss")
    total_load: float = Field(default=0.0, description="S1 + tss")
    load_delta: float = Field(
        default=0.0, description="This minute's total load increment (S1 + tss)"
    )
    tss_delta: float = Field(default=0.0, description="This minute's tss increment")
    total_energy: int = Field(
        default=100, description="Product-facing energy score (0-100)"
    )
    # --- Additional Load Page Requirements ---
    heart_rate_zone: int | None = Field(default=0, description="HR Zone 0 to 5")
    steps: int | None = Field(default=0, description="Steps accumulated this minute")
    calories: float | None = Field(
        default=0.0, description="Calories burned this minute"
    )
    __table_args__ = (
        UniqueConstraint("device_id", "timestamp", name="uix_device_minute_timestamp"),
    )


class HourlyStatus(SQLModel, table=True):
    """Per-hour dynamic status snapshot (boom §5 / temporary §6.3).

    Restart from the frozen morning status each hour; no carry-over of the
    previous hour's penalty. `hour_start` is the local hour bucket.
    """

    id: int | None = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    device: Device = Relationship(back_populates="hourly_status")

    hour_start: NaiveDatetime = Field(
        index=True, description="Local hour bucket start"
    )
    morning_status: int = Field(
        description="Frozen morning status score (restart point)"
    )
    current_status: int = Field(description="Score after this hour's dynamic adjustment")

    # Time-in-zone counts in hours (formal §5). 0 for the temporary path.
    t1: float = Field(
        default=0.0, description="Hours HR below the RHR baseline"
    )
    t2: float = Field(
        default=0.0, description="Hours HR above the RHR baseline"
    )
    t3: float = Field(
        default=0.0, description="Hours HR below baseline - std"
    )
    t4: float = Field(
        default=0.0, description="Hours HR above baseline + std"
    )

    hr_5min_avg: float | None = Field(
        default=None, description="5-min avg non-exercise/non-sleep HR (temporary path)"
    )
    is_sleep_or_exercise: bool = Field(
        default=False, description="True when the hour ends in sleep or exercise"
    )
    is_temporary: bool = Field(
        default=True, description="True if using first-7-days fallback rule"
    )
    __table_args__ = (
        UniqueConstraint("device_id", "hour_start", name="uix_device_hourly_hour"),
    )

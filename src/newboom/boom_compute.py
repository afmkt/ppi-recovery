from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import numpy as np
from sqlmodel import Session, col, select

from newboom.config import (
    ATL_DEFAULT,
    ATL_WINDOW,
    CTL_DEFAULT,
    CTL_WINDOW,
    DEFAULT_HR_MAX,
    DEFAULT_HR_REST,
    ENERGY_CEIL,
    ENERGY_FLOOR,
    FIRST_ENERGY,
    HR_MAX_FROM_AGE,
    LOAD_THRESHOLD_FRAC,
    NIGHT_START_HOUR,
    S1_COEF,
    SIGMOID_M,
    SIGMOID_S,
    STATUS_BASE,
    STATUS_LOAD_DEFAULT,
    STATUS_SLEEP_DEFAULT,
    STATUS_TEMP_FIRST_DAY,
    STATUS_WEIGHT_MATRIX,
    TAU_BASE,
    TAU_LOW,
    TSS_DIV,
    TSS_FEMALE_COEF,
    TSS_FEMALE_EXP,
    TSS_MALE_COEF,
    TSS_MALE_EXP,
    ZONE_FRACTIONS,
    night_key,
)
from newboom.database import engine
from newboom.model import (
    PPI,
    DailySummary,
    Device,
    MinuteMetric,
    Progress,
    Sleep,
    Task,
    WindowMetric,
)

# --- HELPER FUNCTIONS ---


def _sigmoid(total_load: float) -> float:
    """S_(d,t): load-pressure sigmoid (0 when there is no load)."""
    if total_load == 0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-(total_load - SIGMOID_M) / SIGMOID_S))


def _today_local(device: Device | None) -> date:
    """Local 'today' for the device via its fixed-offset tz (naive local if unset)."""
    if not device or not device.tz:
        return datetime.now().date()
    sign = -1 if device.tz.startswith("-") else 1
    hours, minutes = map(int, device.tz.lstrip("+-").split(":"))
    tz = timezone(timedelta(hours=sign * hours, minutes=sign * minutes))
    return datetime.now(UTC).astimezone(tz).date()


def _hr_params(
    device: Device | None, past_summaries: list[DailySummary]
) -> tuple[float, float]:
    """Reference heart-rate range (rest, max) for the load math."""
    if past_summaries and past_summaries[0].hr_max is not None:
        hr_max = past_summaries[0].hr_max
    elif device and device.age:
        hr_max = HR_MAX_FROM_AGE - device.age
    else:
        hr_max = DEFAULT_HR_MAX
    return DEFAULT_HR_REST, hr_max


def _extract_sleep_metrics(
    sleep_record: Sleep | None, night_windows: list[WindowMetric]
) -> dict[str, Any]:
    """Calculates nightly HRV, morning HRV, sleep durations, and bedtime from raw night records."""
    valid_rmssds = [w.rmssd_ms for w in night_windows if w.rmssd_ms is not None]
    hrv_night_val = float(np.mean(valid_rmssds)) if valid_rmssds else None

    # Morning HRV: spec §6 wants the RMSSD after the sleep->wake transition
    # (post-wake, with the interruption conditions in mind). The data model
    # lacks a per-window sleep/awake state (WindowMetric.state is autonomic, not
    # sleep) and per-5s HR, so we approximate with the last 3 windows of the
    # session (end-of-sleep proxy). PENDING: proper post-wake detection.
    morning_windows = night_windows[-3:] if len(night_windows) >= 3 else night_windows
    morning_rmssds = [w.rmssd_ms for w in morning_windows if w.rmssd_ms is not None]
    hrv_morning_val = float(np.mean(morning_rmssds)) if morning_rmssds else None

    rest_hr_val = sleep_record.heart_rate_rest if sleep_record else None

    if sleep_record:
        total_sleep_sec = (
            sleep_record.light_sleep_period
            + sleep_record.deep_sleep_period
            + sleep_record.other_sleep_period
        )
        sleep_duration_h = total_sleep_sec / 3600.0
        deep_sleep_min = sleep_record.deep_sleep_period / 60.0
        sleep_quality = int(np.clip(100 * (total_sleep_sec / (8 * 3600)), 0, 100))

        # Calculate bedtime in hours from midnight (0.0 - 24.0)
        bed_dt = sleep_record.time - timedelta(seconds=sleep_record.sleep_onset_time)
        bed_time_h = bed_dt.hour + bed_dt.minute / 60.0
    else:
        sleep_duration_h = 7.0
        deep_sleep_min = 90.0
        sleep_quality = 75
        bed_time_h = 23.0

    return {
        "hrv_night_val": hrv_night_val,
        "hrv_morning_val": hrv_morning_val,
        "rest_hr_val": rest_hr_val,
        "sleep_duration_h": sleep_duration_h,
        "deep_sleep_min": deep_sleep_min,
        "sleep_quality": sleep_quality,
        "bed_time_h": bed_time_h,
    }


def _compute_rolling_baselines(past_summaries: list[DailySummary]) -> dict[str, Any]:
    """Computes 7-day rolling means and standard deviations from historical summaries."""

    def rolling_stats(values: list[float | None]) -> tuple[float | None, float | None]:
        clean_vals = [v for v in values if v is not None]
        if not clean_vals:
            return None, None
        return float(np.mean(clean_vals)), float(np.std(clean_vals)) if len(
            clean_vals
        ) > 1 else 0.0

    hrv_night_avg, hrv_night_std = rolling_stats(
        [s.hrv_night_avg for s in past_summaries]
    )
    hrv_morning_avg, hrv_morning_std = rolling_stats(
        [s.hrv_morning_avg for s in past_summaries]
    )
    rhr_avg, rhr_std = rolling_stats([s.rest_heart_rate_avg for s in past_summaries])
    bed_time_avg, _ = rolling_stats([s.bed_time_avg for s in past_summaries])

    return {
        "valid_days_count": len(past_summaries),
        "hrv_night_avg": hrv_night_avg,
        "hrv_night_std": hrv_night_std,
        "hrv_morning_avg": hrv_morning_avg,
        "hrv_morning_std": hrv_morning_std,
        "rhr_avg": rhr_avg,
        "rhr_std": rhr_std,
        "bed_time_avg": bed_time_avg,
    }


def _cv_band(std: float | None, avg: float | None) -> int:
    """CV band index for std/avg: 0=<5%, 1=5-10%, 2=10-15%, 3=>15% (boom §1)."""
    if not std or not avg or avg == 0:
        return 0
    ratio = std / avg
    if ratio < 0.05:
        return 0
    if ratio < 0.10:
        return 1
    if ratio < 0.15:
        return 2
    return 3


def _duration_band(hours: float) -> int:
    """Sleep-duration band: 0=>8h, 1=6.5-8h, 2=5-6.5h, 3=<5h (boom §2)."""
    if hours > 8:
        return 0
    if hours >= 6.5:
        return 1
    if hours >= 5:
        return 2
    return 3


def _lateness_band(lateness_min: float) -> int:
    """Bedtime-lateness band: 0=<30min, 1=30-60, 2=60-90, 3=>90 (boom §2)."""
    if lateness_min < 30:
        return 0
    if lateness_min < 60:
        return 1
    if lateness_min < 90:
        return 2
    return 3


def _temporary_status(
    metrics: dict[str, Any],
    baselines: dict[str, Any],
    past_summaries: list[DailySummary],
    load_score: int,
) -> tuple[int, int, int, int, bool]:
    """First-7-days temporary status (boom §6): carry the prior morning score
    forward with fixed add/subtract deltas. The SpO2 delta is pending (no data)."""
    if baselines["valid_days_count"] == 0:
        return (
            STATUS_TEMP_FIRST_DAY,
            STATUS_BASE,
            STATUS_SLEEP_DEFAULT,
            load_score,
            True,
        )

    prev = past_summaries[0]
    prev_score = prev.status_score or STATUS_TEMP_FIRST_DAY

    # Δ_sleep: >=8h +5; 6.5-8h 0; 5-6.5h -5; <5h -10.
    dur = metrics["sleep_duration_h"]
    if dur >= 8:
        delta_sleep = 5
    elif dur >= 6.5:
        delta_sleep = 0
    elif dur >= 5:
        delta_sleep = -5
    else:
        delta_sleep = -10

    # Δ_RHR vs the previous valid day: down >=5bpm +3; up >=5bpm -3; else 0.
    delta_rhr = 0
    if metrics["rest_hr_val"] and prev.rest_heart_rate_avg:
        diff_rhr = metrics["rest_hr_val"] - prev.rest_heart_rate_avg
        if diff_rhr <= -5:
            delta_rhr = 3
        elif diff_rhr >= 5:
            delta_rhr = -3

    # Δ_HRV vs the previous valid day: up >=10% +3; down >=10% -3; else 0.
    delta_hrv = 0
    cur_hrv = metrics["hrv_night_val"]
    prev_hrv = prev.hrv_night_avg
    if cur_hrv and prev_hrv:
        change = (cur_hrv - prev_hrv) / prev_hrv
        if change >= 0.10:
            delta_hrv = 3
        elif change <= -0.10:
            delta_hrv = -3

    # Δ_SpO2 pending: no night-SpO2 in the data model (Sleep has no spo2 field).
    status_score = int(
        np.clip(prev_score + delta_sleep + delta_rhr + delta_hrv, 30, 90)
    )
    return status_score, STATUS_BASE, STATUS_SLEEP_DEFAULT, load_score, True


def _calculate_status_score(
    metrics: dict[str, Any],
    baselines: dict[str, Any],
    past_summaries: list[DailySummary],
) -> tuple[int, int, int, int, bool]:
    """Calculates status, base, sleep, and load scores.

    Formal algorithm (>= 7 valid days with HRV + RHR baselines): base and sleep
    scores from the 4x4 weight matrix (boom §1-§2). Temporary algorithm (first 7
    days): fixed-gear deltas on the prior morning score (boom §6).

    Pending (spec undefined or data unavailable, left as placeholders): the formal
    deduction/bonus items, the SpO2-dependent deltas, the per-hour dynamic
    correction, hrv_morning post-wake, and the recovery_* fields.
    """
    is_temporary = baselines["valid_days_count"] < 7
    load_score = STATUS_LOAD_DEFAULT

    if is_temporary or not (
        baselines["hrv_night_avg"] and baselines["hrv_morning_avg"]
    ):
        return _temporary_status(metrics, baselines, past_summaries, load_score)

    # Formal algorithm: base score from hrv_night & hrv_morning CV bands (§1).
    base_row = _cv_band(baselines["hrv_night_std"], baselines["hrv_night_avg"])
    base_col = _cv_band(baselines["hrv_morning_std"], baselines["hrv_morning_avg"])
    base_score = round(STATUS_BASE / STATUS_WEIGHT_MATRIX[base_row][base_col])

    # Formal algorithm: sleep score from duration band x bedtime-lateness band (§2).
    # Recommended bedtime = 7-day avg bedtime - 1h; lateness = actual - recommended.
    if baselines["bed_time_avg"] is not None:
        lateness_min = (metrics["bed_time_h"] - (baselines["bed_time_avg"] - 1.0)) * 60.0
    else:
        lateness_min = 0.0
    sleep_row = _duration_band(metrics["sleep_duration_h"])
    sleep_col = _lateness_band(lateness_min)
    sleep_score = round(STATUS_BASE / STATUS_WEIGHT_MATRIX[sleep_row][sleep_col])

    # Formal deduction/bonus items are pending (see docstring); score is base+sleep.
    status_score = base_score + sleep_score
    return status_score, base_score, sleep_score, load_score, is_temporary


def _compute_energy_parameters(
    metrics: dict[str, Any],
    baselines: dict[str, Any],
    past_summaries: list[DailySummary],
) -> tuple[float, float]:
    """Computes Body State Modifier T_d and Starting Energy E_d for the energy model."""
    t_d = 1.0
    hrv_m_val = metrics["hrv_morning_val"]
    hrv_m_avg = baselines["hrv_morning_avg"]
    hrv_m_std = baselines["hrv_morning_std"]

    if (
        hrv_m_val
        and hrv_m_avg
        and hrv_m_std
        and hrv_m_std > 0
        and hrv_m_val < (hrv_m_avg - hrv_m_std)
    ):
        t_d = max(1.0, (hrv_m_avg - hrv_m_val) / hrv_m_std)

    start_e = FIRST_ENERGY
    past_f = [s.energy_end_f for s in past_summaries if s.energy_end_f is not None]
    if past_f:
        prev_f = past_f[0]
        if len(past_f) >= 2:
            f_avg = float(np.mean(past_f))
            f_std = float(np.std(past_f))
            tau = TAU_LOW if prev_f < (f_avg - f_std) else TAU_BASE
        else:
            tau = TAU_BASE
        start_e = float(np.clip(1.0 - (1.0 - prev_f) * np.exp(-1.0 / tau), 0.0, 1.0))

    return t_d, start_e


# --- MAIN ORCHESTRATOR FUNCTION ---


def process_daily_summary(device_id: int, target_date: date) -> DailySummary | None:
    """Computes and stores the DailySummary for a specific device and calendar date."""
    with Session(engine) as session:
        # 1. Fetch Sleep and Window Metric records
        sleep_record = session.exec(
            select(Sleep)
            .where(Sleep.device_id == device_id)
            .where(Sleep.night_date == target_date)
        ).first()

        night_windows = session.exec(
            select(WindowMetric)
            .where(WindowMetric.device_id == device_id)
            .where(WindowMetric.night_date == target_date)
            .order_by(col(WindowMetric.window_start).asc())
        ).all()

        if not night_windows:
            return None

        # 2. Extract sleep/HRV metrics & fetch past summaries
        metrics = _extract_sleep_metrics(sleep_record, list(night_windows))

        past_summaries = list(
            session.exec(
                select(DailySummary)
                .where(DailySummary.device_id == device_id)
                .where(DailySummary.summary_date < target_date)
                .order_by(col(DailySummary.summary_date).desc())
                .limit(7)
            ).all()
        )
        device = session.exec(select(Device).where(Device.id == device_id)).first()
        _, hr_max = _hr_params(device, past_summaries)

        # 3. Calculate baselines, status scores, and energy parameters (E_d/T_d); ATL/CTL/TSB finalized per-day
        baselines = _compute_rolling_baselines(past_summaries)
        status_score, base_score, sleep_score, load_score, is_temporary = (
            _calculate_status_score(metrics, baselines, past_summaries)
        )
        t_d, start_e = _compute_energy_parameters(metrics, baselines, past_summaries)

        # Determine bed_time_avg to save as baseline
        bed_time_avg_to_save = (
            baselines["bed_time_avg"]
            if baselines["bed_time_avg"] is not None
            else metrics["bed_time_h"]
        )

        coaching_text = "前几天连续训练负荷较大，HRV较低但储能情况良好，预计明天HRV上升达到超量恢复黄金窗口。建议降低训练量，注意恢复调整。"

        # 4. Upsert DailySummary
        existing = session.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device_id)
            .where(DailySummary.summary_date == target_date)
        ).first()

        if existing:
            existing.hrv_night_avg = metrics["hrv_night_val"]
            existing.hrv_night_std = baselines["hrv_night_std"]
            existing.hrv_morning_avg = metrics["hrv_morning_val"]
            existing.hrv_morning_std = baselines["hrv_morning_std"]
            existing.bed_time_avg = bed_time_avg_to_save
            existing.rest_heart_rate_avg = metrics["rest_hr_val"]
            existing.hr_max = hr_max
            existing.status_score = status_score
            existing.base_score = base_score
            existing.sleep_score = sleep_score
            existing.load_score = load_score
            existing.sleep_quality = metrics["sleep_quality"]
            existing.recovery_sleep_percentage = 55
            existing.recovery_sleep_duration_min = int(metrics["deep_sleep_min"])
            existing.boom_go_text = coaching_text
            existing.supercomp_time = "次日 10:00 – 14:00"
            existing.training_suggestion = "中高强度训练（阈值 / 间歇）"
            existing.training_duration = "60–90分钟"
            existing.training_target = "提升能力上限（耐力/阈值）"
            existing.is_temporary_algorithm = is_temporary
            existing.energy_start_e = start_e
            existing.body_state_modifier_t = t_d
            summary = existing
        else:
            summary = DailySummary(
                device_id=device_id,
                summary_date=target_date,
                hrv_night_avg=metrics["hrv_night_val"],
                hrv_night_std=baselines["hrv_night_std"],
                hrv_morning_avg=metrics["hrv_morning_val"],
                hrv_morning_std=baselines["hrv_morning_std"],
                bed_time_avg=bed_time_avg_to_save,
                rest_heart_rate_avg=metrics["rest_hr_val"],
                hr_max=hr_max,
                status_score=status_score,
                base_score=base_score,
                sleep_score=sleep_score,
                load_score=load_score,
                sleep_quality=metrics["sleep_quality"],
                recovery_sleep_percentage=55,
                recovery_sleep_duration_min=int(metrics["deep_sleep_min"]),
                boom_go_text=coaching_text,
                supercomp_time="次日 10:00 – 14:00",
                training_suggestion="中高强度训练（阈值 / 间歇）",
                training_duration="60–90分钟",
                training_target="提升能力上限（耐力/阈值）",
                is_temporary_algorithm=is_temporary,
                energy_start_e=start_e,
                body_state_modifier_t=t_d,
            )
            session.add(summary)

        session.commit()
        session.refresh(summary)
        return summary


def compute_minute_metrics(
    device_id: int,
    target_date: date,
    timestamp: datetime,
    heart_rate: float,
    is_female: bool,
    hr_rest: float,
    hr_max: float,
    e_d: float,
    t_d: float,
) -> MinuteMetric:
    """Computes the per-minute load increment, cumulative load, and remaining energy.

    Persists per-minute deltas (load_delta, tss_delta) so a day's total load is
    SUM(load_delta). Steps/calories are not modelled and stay zero.
    """
    with Session(engine) as session:
        last_minute = session.exec(
            select(MinuteMetric)
            .where(MinuteMetric.device_id == device_id)
            .where(MinuteMetric.night_date == target_date)
            .order_by(col(MinuteMetric.timestamp).desc())
        ).first()

        prev_s1 = last_minute.s1_load if last_minute else 0.0
        prev_tss = last_minute.tss_load if last_minute else 0.0

        # Heart Rate Zone classification (0 to 5 based on reserve percentages)
        hrr = hr_max - hr_rest
        hr_zone = 0
        if hrr > 0:
            fraction = (heart_rate - hr_rest) / hrr
            for threshold, zone in zip(
                ZONE_FRACTIONS, range(1, len(ZONE_FRACTIONS) + 1)
            ):
                if fraction < threshold:
                    hr_zone = zone
                    break
            else:
                hr_zone = len(ZONE_FRACTIONS) + 1

        # Calculate minute load increment
        threshold_hr = LOAD_THRESHOLD_FRAC * hrr + hr_rest
        delta_s1 = 0.0
        delta_tss = 0.0
        if hrr > 0:
            ratio = (heart_rate - hr_rest) / (LOAD_THRESHOLD_FRAC * hrr)
            if heart_rate < threshold_hr and ratio > 0:
                delta_s1 = S1_COEF * ratio * math.exp(ratio)
            elif heart_rate >= threshold_hr and ratio > 0:
                if is_female:
                    delta_tss = (
                        TSS_FEMALE_COEF * ratio * math.exp(TSS_FEMALE_EXP * ratio)
                    ) / TSS_DIV
                else:
                    delta_tss = (
                        TSS_MALE_COEF * ratio * math.exp(TSS_MALE_EXP * ratio)
                    ) / TSS_DIV

        current_s1 = prev_s1 + delta_s1
        current_tss = prev_tss + delta_tss
        total_load = current_s1 + current_tss

        f_dt = max(
            ENERGY_FLOOR, min(ENERGY_CEIL, e_d - 0.5 * t_d * _sigmoid(total_load))
        )
        total_energy = round(100 * max(0.0, min(1.0, f_dt)))

        minute_metric = MinuteMetric(
            device_id=device_id,
            timestamp=timestamp,
            night_date=target_date,
            heart_rate=heart_rate,
            s1_load=current_s1,
            tss_load=current_tss,
            total_load=total_load,
            total_energy=total_energy,
            heart_rate_zone=hr_zone,
            steps=0,
            calories=0.0,
            load_delta=delta_s1 + delta_tss,
            tss_delta=delta_tss,
        )

        session.add(minute_metric)
        session.commit()
        session.refresh(minute_metric)
        return minute_metric


def process_minute_metric(device_id: int) -> int:
    """
    Processes un-evaluated PPI records into per-minute load/energy metrics.
    Night boundaries use the device's NIGHT_START_HOUR via night_key.
    """
    with Session(engine) as session:
        device = session.exec(select(Device).where(Device.id == device_id)).first()
        is_female = device.is_female if device else False

        # Get or initialize progress tracker for the device
        progress = session.exec(
            select(Progress)
            .where(Progress.device_id == device_id)
            .where(Progress.task == Task.MINUTE_METRIC)
        ).first()

        if not progress:
            progress = Progress(device_id=device_id, task=Task.MINUTE_METRIC)
            session.add(progress)
            session.commit()
            session.refresh(progress)

        start_time = progress.end_time
        if not start_time:
            earliest_ppi = session.exec(
                select(PPI)
                .where(PPI.device_id == device_id)
                .order_by(col(PPI.time).asc())
            ).first()
            start_time = earliest_ppi.time if earliest_ppi else datetime.min

        # Fetch all PPI records newer than the last processed minutely timestamp
        unprocessed_ppi = session.exec(
            select(PPI)
            .where(PPI.device_id == device_id)
            .where(PPI.time > start_time)
            .order_by(col(PPI.time).asc())
        ).all()

        if not unprocessed_ppi:
            return 0

        processed_count = 0
        latest_time = start_time

        for ppi in unprocessed_ppi:
            target_date = night_key(ppi.time, NIGHT_START_HOUR)

            # Day reference values from its summary (defaults if not yet computed).
            summary = session.exec(
                select(DailySummary)
                .where(DailySummary.device_id == device_id)
                .where(DailySummary.summary_date == target_date)
            ).first()
            if summary:
                hr_rest = summary.rest_heart_rate_avg or DEFAULT_HR_REST
                hr_max = summary.hr_max or DEFAULT_HR_MAX
                e_d = (
                    summary.energy_start_e
                    if summary.energy_start_e is not None
                    else FIRST_ENERGY
                )
                t_d = (
                    summary.body_state_modifier_t
                    if summary.body_state_modifier_t is not None
                    else 1.0
                )
            else:
                hr_rest, hr_max = _hr_params(device, [])
                e_d = FIRST_ENERGY
                t_d = 1.0

            compute_minute_metrics(
                device_id=device_id,
                target_date=target_date,
                timestamp=ppi.time,
                heart_rate=float(ppi.hr),
                is_female=is_female,
                hr_rest=hr_rest,
                hr_max=hr_max,
                e_d=e_d,
                t_d=t_d,
            )
            latest_time = ppi.time
            processed_count += 1

        # Update progress tracking
        progress.end_time = latest_time
        session.add(progress)
        session.commit()

        return processed_count


def process_daily_summary_pipeline(device_id: int) -> int:
    """
    Computes daily summaries day-by-day from the earliest available PPI up to the
    device's local today, finalizing each closed day (energy / ATL / CTL / TSB).
    """
    with Session(engine) as session:
        device = session.exec(select(Device).where(Device.id == device_id)).first()

        # 1. Fetch or initialize progress tracker for the DAILY_SUMMARY task
        progress = session.exec(
            select(Progress)
            .where(Progress.device_id == device_id)
            .where(Progress.task == Task.DAILY_SUMMARY)
        ).first()

        # Find the absolute earliest PPI date for this device as our baseline start
        earliest_ppi = session.exec(
            select(PPI).where(PPI.device_id == device_id).order_by(col(PPI.time).asc())
        ).first()

        if not earliest_ppi:
            return 0  # No PPI data available yet to form summaries

        earliest_date = night_key(earliest_ppi.time, NIGHT_START_HOUR)

        if not progress:
            start_date = earliest_date
            progress = Progress(
                device_id=device_id,
                task=Task.DAILY_SUMMARY,
                # end_time holds the last fully-processed local midnight
                end_time=datetime(start_date.year, start_date.month, start_date.day)
                - timedelta(days=1),
            )
            session.add(progress)
            session.commit()
            session.refresh(progress)

        last_processed_dt = progress.end_time or datetime(
            earliest_date.year, earliest_date.month, earliest_date.day
        ) - timedelta(days=1)
        last_processed_date = last_processed_dt.date()

        today = _today_local(device)
        target_date = last_processed_date + timedelta(days=1)

        processed_count = 0
        while target_date <= today:
            # Run the formal / temporary summary calculation for this date
            summary = process_daily_summary(device_id, target_date)
            if summary:
                processed_count += 1
                # Close the day so the next day's E_d chains off this F_d
                if target_date < today:
                    _finalize_one_day(device_id, target_date)

            # Advance progress cursor (naive local midnight, matches storage)
            progress.end_time = datetime(
                target_date.year, target_date.month, target_date.day
            )
            session.add(progress)
            session.commit()

            target_date += timedelta(days=1)

        # Backfill any closed days created earlier that were never finalized.
        finalize_daily_summaries(device_id)

        return processed_count


def _finalize_one_day(device_id: int, target_date: date) -> None:
    """Closes a single day: total load/TS from per-minute deltas, the energy F_d,
    and the ATL/CTL/TSB chain. Idempotent (skips days already closed)."""
    with Session(engine) as session:
        summary = session.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device_id)
            .where(DailySummary.summary_date == target_date)
        ).first()
        if not summary or summary.total_load is not None:
            return

        minutes = session.exec(
            select(MinuteMetric)
            .where(MinuteMetric.device_id == device_id)
            .where(MinuteMetric.night_date == target_date)
        ).all()
        total_load = sum((m.load_delta for m in minutes), 0.0)
        total_tss = sum((m.tss_delta for m in minutes), 0.0)

        e_d = (
            summary.energy_start_e
            if summary.energy_start_e is not None
            else FIRST_ENERGY
        )
        t_d = (
            summary.body_state_modifier_t
            if summary.body_state_modifier_t is not None
            else 1.0
        )
        f_d = max(
            ENERGY_FLOOR, min(ENERGY_CEIL, e_d - 0.5 * t_d * _sigmoid(total_load))
        )

        past = session.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device_id)
            .where(DailySummary.summary_date < target_date)
            .where(col(DailySummary.atl).isnot(None))
            .order_by(col(DailySummary.summary_date).desc())
            .limit(1)
        ).first()
        prev_atl = past.atl if past and past.atl is not None else ATL_DEFAULT
        prev_ctl = past.ctl if past and past.ctl is not None else CTL_DEFAULT
        atl = prev_atl + (total_load - prev_atl) / ATL_WINDOW
        ctl = prev_ctl + (total_load - prev_ctl) / CTL_WINDOW

        summary.total_load = total_load
        summary.total_tss = total_tss
        summary.energy_end_f = f_d
        summary.atl = atl
        summary.ctl = ctl
        summary.tsb = atl - ctl
        session.add(summary)
        session.commit()


def finalize_daily_summaries(device_id: int) -> int:
    """Closes all not-yet-finalized days strictly before the device's local today."""
    with Session(engine) as session:
        device = session.exec(select(Device).where(Device.id == device_id)).first()
        today = _today_local(device)
        open_days = session.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device_id)
            .where(DailySummary.summary_date < today)
            .where(col(DailySummary.total_load).is_(None))
            .order_by(col(DailySummary.summary_date).asc())
        ).all()
        count = 0
        for summary in open_days:
            _finalize_one_day(device_id, summary.summary_date)
            count += 1
        return count

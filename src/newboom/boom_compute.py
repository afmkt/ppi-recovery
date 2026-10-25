from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import numpy as np
from sqlmodel import Session, col, select

from newboom.database import engine
from newboom.model import (
    PPI,
    DailySummary,
    MinuteMetric,
    Progress,
    Sleep,
    Task,
    WindowMetric,
)

# --- HELPER FUNCTIONS ---


def _extract_sleep_metrics(
    sleep_record: Sleep | None, night_windows: list[WindowMetric]
) -> dict[str, Any]:
    """Calculates nightly HRV, morning HRV, sleep durations, and bedtime from raw night records."""
    valid_rmssds = [w.rmssd_ms for w in night_windows if w.rmssd_ms is not None]
    hrv_night_val = float(np.mean(valid_rmssds)) if valid_rmssds else None

    # Morning HRV (last 3 windows of the sleep session)
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


def _calculate_status_score(
    metrics: dict[str, Any],
    baselines: dict[str, Any],
    past_summaries: list[DailySummary],
) -> tuple[int, int, int, int, bool]:
    """Calculates status, base, sleep, and load scores for formal vs. temporary algorithm."""
    is_temporary = baselines["valid_days_count"] < 7
    base_score = 50
    sleep_score = 50
    load_score = 65

    if not is_temporary and baselines["hrv_night_avg"] and baselines["rhr_avg"]:
        # Formal Algorithm Calculation (placeholder score)
        status_score = 75
    else:
        # Temporary Algorithm Calculation (first 7 days fallback)
        if baselines["valid_days_count"] == 0:
            status_score = 60
        else:
            prev_score = past_summaries[0].status_score or 60
            delta_sleep = (
                5
                if metrics["sleep_duration_h"] >= 8
                else (-5 if metrics["sleep_duration_h"] < 5 else 0)
            )

            delta_rhr = 0
            if metrics["rest_hr_val"] and past_summaries[0].rest_heart_rate_avg:
                diff_rhr = (
                    metrics["rest_hr_val"] - past_summaries[0].rest_heart_rate_avg
                )
                delta_rhr = 3 if diff_rhr <= -5 else (-3 if diff_rhr >= 5 else 0)

            status_score = int(np.clip(prev_score + delta_sleep + delta_rhr, 30, 90))

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

    start_e = 1.0
    if past_summaries:
        prev_f = past_summaries[0].energy_end_f
        t_effective = 3.0 if prev_f < 0.5 else 1.5
        start_e = float(
            np.clip(1.0 - (1.0 - prev_f) * np.exp(-1.0 / t_effective), 0.0, 1.0)
        )

    return t_d, start_e


def _compute_load_training_metrics(
    past_summaries: list[DailySummary], today_load: float = 55.0
) -> tuple[float, float, float]:
    """Computes short-term load (ATL), long-term load (CTL), and Training Stress Balance (TSB)."""
    yesterday_atl = (
        past_summaries[0].atl
        if past_summaries and past_summaries[0].atl is not None
        else 37.0
    )
    yesterday_ctl = (
        past_summaries[0].ctl
        if past_summaries and past_summaries[0].ctl is not None
        else 54.0
    )

    atl = yesterday_atl + (today_load - yesterday_atl) / 7.0
    ctl = yesterday_ctl + (today_load - yesterday_ctl) / 42.0
    tsb = ctl - atl

    return atl, ctl, tsb


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

        # 3. Calculate baselines, scores, energy, and load metrics
        baselines = _compute_rolling_baselines(past_summaries)
        status_score, base_score, sleep_score, load_score, is_temporary = (
            _calculate_status_score(metrics, baselines, past_summaries)
        )
        t_d, start_e = _compute_energy_parameters(metrics, baselines, past_summaries)
        atl, ctl, tsb = _compute_load_training_metrics(past_summaries)

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
            existing.hr_max = (
                past_summaries[0].hr_max if past_summaries else existing.hr_max
            )
            existing.status_score = status_score
            existing.base_score = base_score
            existing.sleep_score = sleep_score
            existing.load_score = load_score
            existing.sleep_quality = metrics["sleep_quality"]
            existing.recovery_sleep_percentage = 55
            existing.recovery_sleep_duration_min = int(metrics["deep_sleep_min"])
            existing.atl = atl
            existing.ctl = ctl
            existing.tsb = tsb
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
                hr_max=past_summaries[0].hr_max if past_summaries else 190.0,
                status_score=status_score,
                base_score=base_score,
                sleep_score=sleep_score,
                load_score=load_score,
                sleep_quality=metrics["sleep_quality"],
                recovery_sleep_percentage=55,
                recovery_sleep_duration_min=int(metrics["deep_sleep_min"]),
                atl=atl,
                ctl=ctl,
                tsb=tsb,
                boom_go_text=coaching_text,
                supercomp_time="次日 10:00 – 14:00",
                training_suggestion="中高强度训练（阈值 / 间歇）",
                training_duration="60–90分钟",
                training_target="提升能力上限（耐力/阈值）",
                is_temporary_algorithm=is_temporary,
                energy_start_e=start_e,
                energy_end_f=start_e,
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
    is_female: bool = False,
    steps_delta: int = 10,
    calories_delta: float = 2.5,
) -> MinuteMetric:
    """
    Computes minutely load (S1 + tss), remaining energy (totalEnergy), heart rate zones,
    steps, and calories for a given minute timestamp.
    """
    with Session(engine) as session:
        summary = session.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device_id)
            .where(DailySummary.summary_date == target_date)
        ).first()

        if not summary:
            hr_rest = 60.0
            hr_max = 190.0
            e_d = 1.0
            t_d = 1.0
        else:
            hr_rest = summary.rest_heart_rate_avg or 60.0
            hr_max = summary.hr_max or 190.0
            e_d = summary.energy_start_e
            t_d = summary.body_state_modifier_t

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
            if fraction < 0.5:
                hr_zone = 1
            elif fraction < 0.6:
                hr_zone = 2
            elif fraction < 0.7:
                hr_zone = 3
            elif fraction < 0.85:
                hr_zone = 4
            else:
                hr_zone = 5

        # Calculate minute load increment
        threshold_hr = 0.45 * hrr + hr_rest
        delta_s1 = 0.0
        delta_tss = 0.0

        if hrr > 0:
            ratio = (heart_rate - hr_rest) / (0.45 * hrr)
            if heart_rate < threshold_hr and ratio > 0:
                delta_s1 = 1.0 * 0.6 * ratio * math.exp(ratio)
            elif heart_rate >= threshold_hr and ratio > 0:
                if is_female:
                    delta_tss = (1.0 * 0.5 * ratio * math.exp(1.64 * ratio)) / 60.0
                else:
                    delta_tss = (1.0 * 0.4 * ratio * math.exp(1.92 * ratio)) / 60.0

        current_s1 = prev_s1 + delta_s1
        current_tss = prev_tss + delta_tss
        total_load = current_s1 + current_tss

        # Sigmoid pressure conversion
        if total_load == 0:
            s_dt = 0.0
        else:
            s_dt = 1.0 / (1.0 + math.exp(-(total_load - 125.0) / 45.0))

        f_dt = max(-0.1, min(1.0, e_d - 0.5 * t_d * s_dt))
        total_energy = round(100 * max(0.0, min(1.0, f_dt)))

        if summary:
            summary.energy_end_f = f_dt
            session.add(summary)

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
            steps=steps_delta,
            calories=calories_delta,
        )

        session.add(minute_metric)
        session.commit()
        session.refresh(minute_metric)
        return minute_metric


def process_minute_metric(device_id: int) -> int:
    """
    Processes un-evaluated PPI records into minutely metrics,
    ensuring daily summaries are computed once per unique date.
    """
    with Session(engine) as session:
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

        # Use a dedicated tracker field (e.g., last_minute_time) or fallback to earliest PPI
        start_time = progress.end_time
        if not start_time:
            earliest_ppi = session.exec(
                select(PPI)
                .where(PPI.device_id == device_id)
                .order_by(col(PPI.time).asc())
            ).first()
            start_time = (
                earliest_ppi.time
                if earliest_ppi
                else datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
            )

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

        # 2. Loop through PPI records to compute minutely metrics
        for ppi in unprocessed_ppi:
            target_date = ppi.time.date()

            is_active = ppi.activity >= 4
            steps_delta = 12 if is_active else 0
            calories_delta = 3.5 if is_active else 1.2

            compute_minute_metrics(
                device_id=device_id,
                target_date=target_date,
                timestamp=ppi.time,
                heart_rate=float(ppi.hr),
                is_female=False,
                steps_delta=steps_delta,
                calories_delta=calories_delta,
            )
            latest_time = ppi.time
            processed_count += 1

        # 3. Update progress tracking
        progress.end_time = latest_time
        session.add(progress)
        session.commit()

        return processed_count


def process_daily_summary_pipeline(device_id: int) -> int:
    """
    Contiguously computes daily summaries day-by-day from the earliest available
    PPI date up to the current date using the task-keyed Progress table.
    """
    with Session(engine) as session:
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

        earliest_date = earliest_ppi.time.date()

        if not progress:
            # Start tracking from the day before earliest PPI (or the earliest date itself)
            start_date = earliest_date
            progress = Progress(
                device_id=device_id,
                task=Task.DAILY_SUMMARY,
                # We can store date-as-datetime or use end_time to represent the last fully processed date
                end_time=datetime(
                    start_date.year,
                    start_date.month,
                    start_date.day,
                    tzinfo=UTC,
                )
                - timedelta(days=1),
            )
            session.add(progress)
            session.commit()
            session.refresh(progress)

        last_processed_dt = progress.end_time or datetime(
            earliest_date.year,
            earliest_date.month,
            earliest_date.day,
            tzinfo=UTC,
        ) - timedelta(days=1)
        last_processed_date = last_processed_dt.date()

        today = datetime.now(UTC).date()

        # We process up to yesterday or today depending on whether the day is complete.
        # Let's process up to today's date (or you can restrict to < today if you only summarize finished days).
        target_date = last_processed_date + timedelta(days=1)

        processed_count = 0
        while target_date <= today:
            # Run the formal / temporary summary calculation for this specific calendar date
            summary = process_daily_summary(device_id, target_date)

            if summary:
                processed_count += 1

            # Advance progress cursor
            progress.end_time = datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                tzinfo=UTC,
            )
            session.add(progress)
            session.commit()

            target_date += timedelta(days=1)

        return processed_count

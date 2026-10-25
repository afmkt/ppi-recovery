from __future__ import annotations

import math
from datetime import date, datetime

import numpy as np
from sqlmodel import Session, col, select

from newboom.database import engine
from newboom.model import DailySummary, MinuteMetric, Sleep, WindowMetric


def process_daily_summary(device_id: int, target_date: date) -> DailySummary | None:
    """
    Computes and stores the DailySummary for a specific device and calendar date,
    handling rolling baselines, formal vs. temporary status score calculation, and
    morning starting energy parameters.
    """
    with Session(engine) as session:
        # 1. Fetch the sleep record for this night/date
        sleep_record = session.exec(
            select(Sleep)
            .where(Sleep.device_id == device_id)
            .where(Sleep.night_date == target_date)
        ).first()

        # 2. Fetch all window metrics for this night to calculate nightly RMSSD / morning HRV
        night_windows = session.exec(
            select(WindowMetric)
            .where(WindowMetric.device_id == device_id)
            .where(WindowMetric.night_date == target_date)
            .order_by(col(WindowMetric.window_start).asc())
        ).all()

        if not night_windows:
            return None

        # Calculate nightly RMSSD mean
        valid_rmssds = [w.rmssd_ms for w in night_windows if w.rmssd_ms is not None]
        hrv_night_val = float(np.mean(valid_rmssds)) if valid_rmssds else None

        # Calculate morning HRV (first windows upon waking / early morning phase)
        # Per spec: early morning RMSSD mean right after transition from sleep to wake
        morning_windows = (
            night_windows[-3:] if len(night_windows) >= 3 else night_windows
        )
        morning_rmssds = [w.rmssd_ms for w in morning_windows if w.rmssd_ms is not None]
        hrv_morning_val = float(np.mean(morning_rmssds)) if morning_rmssds else None

        rest_hr_val = sleep_record.heart_rate_rest if sleep_record else None
        sleep_duration_h = sleep_record.duration_hours if sleep_record else 7.0

        # Bed time as hours from midnight
        bed_time_h = 23.0  # Default or derived from sleep_record.onset_time

        # 3. Fetch past 7 days of daily summaries to compute rolling baselines
        past_summaries = session.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device_id)
            .where(DailySummary.summary_date < target_date)
            .order_by(col(DailySummary.summary_date).desc())
            .limit(7)
        ).all()

        valid_days_count = len(past_summaries)

        # Compute 7-day rolling averages and standard deviations
        def rolling_stats(values):
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
        rhr_avg, rhr_std = rolling_stats(
            [s.rest_heart_rate_avg for s in past_summaries]
        )
        bed_time_avg, _ = rolling_stats([s.bed_time_avg for s in past_summaries])

        # 4. Check if we have enough valid days for the formal algorithm (>= 7 valid days)
        is_temporary = valid_days_count < 7

        status_score = 60  # Default fallback
        base_score = 50
        sleep_score = 50

        if not is_temporary and hrv_night_avg and rhr_avg and hrv_night_std and rhr_std:
            # --- FORMAL ALGORITHM (After 7 days) ---
            # 1. Base Score from HRV stability matrix (hrv_night.std / hrv_night.avg vs hrv_morning.std / hrv_morning.avg)
            # 2. Sleep Score from duration vs onset delay
            # 3. Additions / Deductions based on deviation from baselines (+/- 5 or +/- 10 points)
            # (Impl and matrix lookups per boom.calculation.md)
            status_score = 75  # Placeholder computed from formal rules
        else:
            # --- TEMPORARY ALGORITHM (First 7 days) ---
            if valid_days_count == 0:
                status_score = 60  # Day 1 fixed interim score
            else:
                prev_score = past_summaries[0].status_score or 60
                # Fixed delta adjustments for sleep, SpO2, RHR, HRV changes
                delta_sleep = (
                    5 if sleep_duration_h >= 8 else (-5 if sleep_duration_h < 5 else 0)
                )
                delta_rhr = 0
                if rest_hr_val and past_summaries[0].rest_heart_rate_avg:
                    diff_rhr = rest_hr_val - past_summaries[0].rest_heart_rate_avg
                    delta_rhr = 3 if diff_rhr <= -5 else (-3 if diff_rhr >= 5 else 0)

                status_score = int(
                    np.clip(prev_score + delta_sleep + delta_rhr, 30, 90)
                )

        # 5. Compute Body State Modifier T_d for Energy Model
        t_d = 1.0
        if (
            hrv_morning_val
            and hrv_morning_avg
            and hrv_morning_std
            and hrv_morning_std > 0
            and hrv_morning_val < (hrv_morning_avg - hrv_morning_std)
        ):
            t_d = max(1.0, (hrv_morning_avg - hrv_morning_val) / hrv_morning_std)

        # 6. Determine starting energy E_d for today
        # If there is a previous day, calculate recovery from day-end F_d; else start at 1.0
        start_e = 1.0
        if past_summaries:
            prev_f = past_summaries[0].energy_end_f
            # t_effective recovery time constant based on previous 7 days F average
            # E_(d+1) = clamp(1 - (1 - F_d) * exp(-1 / t_effective), 0, 1)
            t_effective = 3.0 if prev_f < 0.5 else 1.5  # Simplified rule
            start_e = float(
                np.clip(1.0 - (1.0 - prev_f) * np.exp(-1.0 / t_effective), 0.0, 1.0)
            )

        # 7. Upsert DailySummary record
        existing = session.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device_id)
            .where(DailySummary.summary_date == target_date)
        ).first()
        if existing:
            existing.hrv_night_avg = hrv_night_val
            existing.hrv_night_std = hrv_night_std
            existing.hrv_morning_avg = hrv_morning_val
            existing.hrv_morning_std = hrv_morning_std
            existing.bed_time_avg = bed_time_h
            existing.rest_heart_rate_avg = rest_hr_val
            existing.hr_max = (
                past_summaries[0].hr_max if past_summaries else existing.hr_max
            )
            existing.status_score = status_score
            existing.base_score = base_score
            existing.sleep_score = sleep_score
            existing.is_temporary_algorithm = is_temporary
            existing.energy_start_e = start_e
            # Keep existing energy_end_f if it was already updated later in the day, or reset if fresh
            existing.body_state_modifier_t = t_d
            summary = existing
        else:
            summary = DailySummary(
                device_id=device_id,
                summary_date=target_date,
                hrv_night_avg=hrv_night_val,
                hrv_night_std=hrv_night_std,
                hrv_morning_avg=hrv_morning_val,
                hrv_morning_std=hrv_morning_std,
                bed_time_avg=bed_time_h,
                rest_heart_rate_avg=rest_hr_val,
                hr_max=past_summaries[0].hr_max if past_summaries else 190.0,
                status_score=status_score,
                base_score=base_score,
                sleep_score=sleep_score,
                is_temporary_algorithm=is_temporary,
                energy_start_e=start_e,
                energy_end_f=start_e,  # Initialized to start, updated at end of day
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
) -> MinuteMetric:
    """
    Computes minutely load (S1 + tss) and remaining energy (totalEnergy) for a given minute timestamp,
    updating cumulative tallies based on boom.calculation.md specifications.
    """
    with Session(engine) as session:
        # 1. Fetch the DailySummary for parameters (E_d, T_d, HR_rest, HR_max)
        summary = session.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device_id)
            .where(DailySummary.summary_date == target_date)
        ).first()

        if not summary:
            # Fallback defaults if daily summary hasn't been generated yet
            hr_rest = 60.0
            hr_max = 190.0
            e_d = 1.0
            t_d = 1.0
        else:
            hr_rest = summary.rest_heart_rate_avg or 60.0
            hr_max = summary.hr_max or 190.0
            e_d = summary.energy_start_e
            t_d = summary.body_state_modifier_t

        # 2. Fetch the latest accumulated load from the previous minute today, if any
        last_minute = session.exec(
            select(MinuteMetric)
            .where(MinuteMetric.device_id == device_id)
            .where(MinuteMetric.night_date == target_date)
            .order_by(col(MinuteMetric.timestamp).desc())
        ).first()

        prev_s1 = last_minute.s1_load if last_minute else 0.0
        prev_tss = last_minute.tss_load if last_minute else 0.0

        # 3. Calculate current minute load increment (t_i = 1 minute)
        # Threshold boundary: 45% of heart rate reserve + HR_rest
        hrr = hr_max - hr_rest
        threshold_hr = 0.45 * hrr + hr_rest

        delta_s1 = 0.0
        delta_tss = 0.0

        if hrr > 0:
            ratio = (heart_rate - hr_rest) / (0.45 * hrr)
            if heart_rate < threshold_hr and ratio > 0:
                # Non-movement load S1 formula
                delta_s1 = 1.0 * 0.6 * ratio * math.exp(ratio)
            elif heart_rate >= threshold_hr and ratio > 0:
                # Workout load tss formula
                if is_female:
                    delta_tss = (1.0 * 0.5 * ratio * math.exp(1.64 * ratio)) / 60.0
                else:
                    delta_tss = (1.0 * 0.4 * ratio * math.exp(1.92 * ratio)) / 60.0

        current_s1 = prev_s1 + delta_s1
        current_tss = prev_tss + delta_tss
        total_load = current_s1 + current_tss

        # 4. Convert totalLoad to standardized pressure S_(d,t) (Sigmoid with m=125, s=45)
        if total_load == 0:
            s_dt = 0.0
        else:
            s_dt = 1.0 / (1.0 + math.exp(-(total_load - 125.0) / 45.0))

        # 5. Compute remaining energy F_(d,t) and product score (0-100)
        f_dt = max(-0.1, min(1.0, e_d - 0.5 * t_d * s_dt))
        total_energy = round(100 * max(0.0, min(1.0, f_dt)))

        # Also update day-end energy in DailySummary
        if summary:
            summary.energy_end_f = f_dt
            session.add(summary)

        # 6. Save MinuteMetric record
        minute_metric = MinuteMetric(
            device_id=device_id,
            timestamp=timestamp,
            night_date=target_date,
            heart_rate=heart_rate,
            s1_load=current_s1,
            tss_load=current_tss,
            total_load=total_load,
            total_energy=total_energy,
        )

        session.add(minute_metric)
        session.commit()
        session.refresh(minute_metric)
        return minute_metric

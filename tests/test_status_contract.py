"""P1-2: the temporary-vs-formal status contract and the per-hourly restart.

The prose rules (boom §6) are pinned here: the first valid day is a fixed 60,
later days carry the *prior morning* score forward with clamped fixed deltas,
and the hourly snapshot always restarts from the frozen morning score so a
prior-hour penalty never carries into the next hour or day.
"""
from datetime import date, datetime

from newboom.boom_compute import (
    _calculate_status_score,
    _deduction_bonus,
    _resolve_hr_rest,
    _temporary_status,
    process_hourly_status,
)
from newboom.config import DEFAULT_HR_REST, STATUS_TEMP_FIRST_DAY
from newboom.database import engine
from newboom.model import (
    ActivityState,
    DailySummary,
    Device,
    HourlyStatus,
    PPI,
)
from sqlmodel import Session, select


# --- pure helpers ----------------------------------------------------------


def _summary(**kw):
    base = dict(device_id=1, summary_date=date(2026, 10, 1))
    base.update(kw)
    return DailySummary(**base)


def _tmp_metrics(**kw):
    """Neutral temporary-path metrics (every delta 0) unless overridden."""
    m = {
        "sleep_duration_h": 7.0,  # 6.5-8h -> delta_sleep 0
        "rest_hr_val": None,  # -> delta_rhr 0
        "hrv_night_val": None,  # -> delta_hrv 0
        "spo2_night_avg": None,  # -> delta_spo2 0
    }
    m.update(kw)
    return m


def _full_metrics():
    return {
        "hrv_night_val": 50.0,
        "hrv_morning_val": 40.0,
        "rest_hr_val": 60.0,
        "spo2_night_avg": 97.0,
        "sleep_duration_h": 7.0,
        "bed_time_h": 23.0,
        "sleep_quality": 80,
        "deep_sleep_min": 90.0,
    }


def _full_baselines(valid_days=7):
    return {
        "valid_days_count": valid_days,
        "hrv_night_avg": 50.0,
        "hrv_night_std": 5.0,
        "hrv_morning_avg": 40.0,
        "hrv_morning_std": 4.0,
        "rhr_avg": 60.0,
        "rhr_std": 3.0,
        "bed_time_avg": 23.0,
        "nonsport_hr_avg": 70.0,
        "nonsport_hr_std": 5.0,
    }


# --- P1-2 temporary status --------------------------------------------------


def test_temporary_first_day_is_fixed_60():
    out = _temporary_status(_tmp_metrics(), {"valid_days_count": 0}, [], 65)
    assert out[0] == STATUS_TEMP_FIRST_DAY
    assert out[4] is True  # is_temporary


def test_temporary_clamps_high():
    prev = _summary(status_score=88)
    out = _temporary_status(
        _tmp_metrics(sleep_duration_h=8.5), {"valid_days_count": 3}, [prev], 65
    )
    assert out[0] == 90


def test_temporary_clamps_low():
    prev = _summary(status_score=32)
    out = _temporary_status(
        _tmp_metrics(sleep_duration_h=4.0), {"valid_days_count": 3}, [prev], 65
    )
    assert out[0] == 30


def test_temporary_missing_data_is_zero_delta():
    # None SpO2 / HRV / RHR -> that factor contributes 0, not a 0-value substitute.
    prev = _summary(status_score=60, rest_heart_rate_val=60, hrv_night_val=50)
    out = _temporary_status(_tmp_metrics(), {"valid_days_count": 3}, [prev], 65)
    assert out[0] == 60


def test_temporary_no_carry_uses_morning_score():
    # Next-day base is the prior MORNING score, never the hourly-adjusted current.
    prev = _summary(status_score=50, status_current=30)
    out = _temporary_status(_tmp_metrics(), {"valid_days_count": 3}, [prev], 65)
    assert out[0] == 50


# --- P1-2 formal switch -----------------------------------------------------


def test_status_formal_at_seven_days():
    out = _calculate_status_score(_full_metrics(), _full_baselines(7), [])
    assert out[4] is False  # is_temporary


def test_status_temporary_before_seven_days():
    out = _calculate_status_score(
        _full_metrics(),
        _full_baselines(3),
        [_summary(status_score=60)],
    )
    assert out[4] is True


def _seed_device(mac: str) -> int:
    with Session(engine) as s:
        d = Device(name="t", mac=mac, age=30)
        s.add(d)
        s.commit()
        return int(d.id)
# --- §3-§4 deduction / bonus -----------------------------------------------


def test_deduction_worst_tier_summed():
    metrics = {
        "rest_hr_val": 70.0,
        "hrv_night_val": 40.0,
        "hrv_morning_val": 30.0,
        "spo2_night_avg": 88.0,
    }
    baselines = {
        "rhr_avg": 60.0,
        "rhr_std": 5.0,
        "hrv_night_avg": 50.0,
        "hrv_night_std": 5.0,
        "hrv_morning_avg": 40.0,
        "hrv_morning_std": 5.0,
    }
    deduction, bonus, adjustment = _deduction_bonus(metrics, baselines)
    assert deduction == -40
    assert bonus == 0
    assert adjustment == -40


def test_bonus_worst_tier_summed():
    metrics = {"rest_hr_val": 50.0, "hrv_night_val": 60.0, "hrv_morning_val": 50.0}
    baselines = {
        "rhr_avg": 60.0,
        "rhr_std": 5.0,
        "hrv_night_avg": 50.0,
        "hrv_night_std": 5.0,
        "hrv_morning_avg": 40.0,
        "hrv_morning_std": 5.0,
    }
    deduction, bonus, adjustment = _deduction_bonus(metrics, baselines)
    assert bonus == 30
    assert deduction == 0
    assert adjustment == 30


# --- P0-2 RHR baseline fallback chain --------------------------------------


def test_resolve_hr_rest_prefers_night_baseline():
    night = date(2026, 10, 27)
    summary = _summary(summary_date=night, rest_heart_rate_avg=58.0)
    assert _resolve_hr_rest(night, summary, None, {}) == 58.0


def test_resolve_hr_rest_falls_back_to_latest_closed():
    night = date(2026, 10, 27)
    latest = _summary(summary_date=date(2026, 10, 26), rest_heart_rate_avg=55.0)
    assert _resolve_hr_rest(night, None, latest, {}) == 55.0


def test_resolve_hr_rest_night_baseline_none_uses_latest():
    night = date(2026, 10, 27)
    summary = _summary(summary_date=night, rest_heart_rate_avg=None)
    latest = _summary(summary_date=date(2026, 10, 26), rest_heart_rate_avg=55.0)
    assert _resolve_hr_rest(night, summary, latest, {}) == 55.0


def test_resolve_hr_rest_falls_back_to_sleep():
    night = date(2026, 10, 27)
    assert _resolve_hr_rest(night, None, None, {night: 52.0}) == 52.0


def test_resolve_hr_rest_default_last():
    night = date(2026, 10, 27)
    assert _resolve_hr_rest(night, None, None, {}) == DEFAULT_HR_REST


# --- P1-2 hourly restart (DB-backed) ---------------------------------------


def _seed_device(mac: str) -> int:
    with Session(engine) as s:
        d = Device(name="t", mac=mac, age=30)
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _seed_morning_daily(device_id: int) -> None:
    with Session(engine) as s:
        s.add(
            DailySummary(
                device_id=device_id,
                summary_date=date(2026, 10, 27),
                status_score=50,
                status_morning=50,
                status_current=30,  # a prior-hour penalty had dropped it
                is_temporary_algorithm=True,
                rest_heart_rate_avg=60.0,
                hr_max=190.0,
            )
        )
        s.commit()


def _seed_high_hr_hour(device_id: int, hour: int, hr: int = 110) -> None:
    # PPI in the last minutes of the hour so the §6.3 5-min window
    # ([hour_end-5m, hour_end)) contains them; high enough to trip the -5 tier.
    with Session(engine) as s:
        for m in (55, 56, 57):
            s.add(
                PPI(
                    device_id=device_id,
                    time=datetime(2026, 10, 27, hour, m),
                    hr=hr,
                    spo2=98,
                    ppi=1000,
                    activity=ActivityState.RELAXED,
                    fresh=False,
                )
            )
        s.commit()


def _hourly(device_id: int, hour: int) -> HourlyStatus:
    with Session(engine) as s:
        return s.exec(
            select(HourlyStatus)
            .where(HourlyStatus.device_id == device_id)
            .where(HourlyStatus.hour_start == datetime(2026, 10, 27, hour, 0))
        ).one()


def test_hourly_restart_from_morning_no_carry():
    dev_id = _seed_device("00:00:00:00:00:01")
    _seed_morning_daily(dev_id)

    # Hour 10: high non-sport HR (> RHR+40) -> -5, restarting from the morning 50.
    _seed_high_hr_hour(dev_id, 10)
    assert process_hourly_status(dev_id) == 1
    hs10 = _hourly(dev_id, 10)
    assert hs10.morning_status == 50
    assert hs10.current_status == 45

    # Hour 11: the snapshot must restart from the morning 50, not the prior
    # hour's current 45 (no carry of the prior-hour penalty).
    _seed_high_hr_hour(dev_id, 11)
    assert process_hourly_status(dev_id) == 1
    hs11 = _hourly(dev_id, 11)
    assert hs11.morning_status == 50
    assert hs11.current_status == 45

    # Re-running the same hour is a no-op (hour-boundary gate).
    assert process_hourly_status(dev_id) == 0

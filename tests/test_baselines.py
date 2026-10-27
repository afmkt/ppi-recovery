"""P0-1: DailySummary baseline fields.

Per spec §基准值, `*_avg`/`*_std` are the 7-day rolling baseline; the per-day
*raw* value is a separate field (`*_val`). The processor must store the rolling
stat under `*_avg` and today's raw value under `*_val`, and the rolling must be
computed from the per-day `*_val` fields, never the stale `*_avg`.
"""
from datetime import date, datetime, timedelta

import numpy as np
from sqlmodel import Session, select

from newboom.boom_compute import _compute_rolling_baselines, process_daily_summary
from newboom.database import engine
from newboom.model import (
    DailySummary,
    Device,
    Sleep,
    WindowMetric,
    WindowState,
)


def _night_windows(
    session: Session,
    device_id: int,
    night: date,
    count: int,
    rmssd: float,
) -> None:
    night_start = datetime(night.year, night.month, night.day, 8, 0)
    for i in range(count):
        session.add(
            WindowMetric(
                device_id=device_id,
                night_date=night,
                window_start=night_start + timedelta(minutes=5 * i),
                window_end=night_start + timedelta(minutes=5 * i + 5),
                window_mid_h=(5 * i) / 60.0,
                valid_ratio=1.0,
                rmssd_ms=rmssd,
                ln_rmssd=float(np.log(rmssd)),
                state=WindowState.PARASYMPATHETIC,
                is_sleep=True,
                fresh=False,
            )
        )


def test_rolling_baselines_read_per_day_val_not_avg():
    # Even if the *_avg fields hold garbage, the rolling uses *_val.
    d1 = DailySummary(device_id=1, summary_date=date(2026, 10, 25), hrv_night_val=50.0)
    d2 = DailySummary(device_id=1, summary_date=date(2026, 10, 26), hrv_night_val=60.0)
    d1.hrv_night_avg = 999.0
    d2.hrv_night_avg = 999.0
    out = _compute_rolling_baselines([d2, d1])
    assert out["hrv_night_avg"] == 55.0
    assert out["valid_days_count"] == 2


def test_daily_summary_stores_rolling_avg_and_raw_val():
    with Session(engine) as s:
        dev = Device(name="t", mac="00:00:00:00:00:07", age=30)
        s.add(dev)
        s.commit()
        device_id = int(dev.id)
        # Past per-day raw values feed the 7-day rolling baselines.
        s.add(
            DailySummary(
                device_id=device_id,
                summary_date=date(2026, 10, 25),
                hrv_night_val=50.0,
                rest_heart_rate_val=50.0,
            )
        )
        s.add(
            DailySummary(
                device_id=device_id,
                summary_date=date(2026, 10, 26),
                hrv_night_val=60.0,
                rest_heart_rate_val=60.0,
            )
        )
        s.commit()
        # Tonight's 3 sleep windows @ RMSSD 70 -> today's raw night HRV.
        _night_windows(s, device_id, date(2026, 10, 27), 3, 70.0)
        s.commit()

    process_daily_summary(device_id, date(2026, 10, 27))
    with Session(engine) as s:
        summary = s.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device_id)
            .where(DailySummary.summary_date == date(2026, 10, 27))
        ).one()
        assert summary is not None
        # raw today's night HRV, kept separate from the rolling baseline
        assert summary.hrv_night_val == 70.0
        # 7-day rolling of the past per-day raw values [50, 60]
        assert summary.hrv_night_avg == 55.0
        assert summary.rest_heart_rate_avg == 55.0


def test_rolling_std_is_zero_for_single_day():
    d1 = DailySummary(device_id=1, summary_date=date(2026, 10, 25), hrv_night_val=50.0)
    out = _compute_rolling_baselines([d1])
    assert out["hrv_night_std"] == 0.0


def test_daily_summary_loads_sleep_rest_hr():
    # Regression: the night's Sleep record must be found by the time window,
    # so its rest HR (and the derived daily metrics) are actually loaded.
    with Session(engine) as s:
        dev = Device(name="t", mac="00:00:00:00:00:0C", age=30)
        s.add(dev)
        s.commit()
        device_id = int(dev.id)
        _night_windows(s, device_id, date(2026, 10, 27), 3, 70.0)
        s.add(
            Sleep(
                device_id=device_id,
                timezone="UTC",
                time=datetime(2026, 10, 27, 23, 0),
                sleep_onset_time=0,
                awake_time=0,
                light_sleep_period=6 * 3600,
                deep_sleep_period=1 * 3600,
                other_sleep_period=0,
                heart_rate_rest=58,
                fresh=False,
            )
        )
        s.commit()

    process_daily_summary(device_id, date(2026, 10, 27))
    with Session(engine) as s:
        summary = s.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device_id)
            .where(DailySummary.summary_date == date(2026, 10, 27))
        ).one()
        assert summary is not None
        assert summary.rest_heart_rate_val == 58.0

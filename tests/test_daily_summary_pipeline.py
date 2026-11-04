"""Regression: the daily-summary pipeline must recompute the *open* (current)
day on every pass, even when the DAILY_SUMMARY cursor already sits at today's
midnight.

Before the fix, ``process_daily_summary_pipeline`` advanced its cursor past
``today`` even when ``process_daily_summary`` returned ``None`` — the open day
was empty at local 00:00, so the cursor jumped to today with no summary, and the
day was never revisited. An active device that started streaming mid-day was
frozen at its empty 00:00 snapshot forever, and ``/go`` (which reads the latest
DailySummary) returned all zeros despite the device having plenty of data.
"""
from datetime import date, datetime, timedelta

import numpy as np
from sqlmodel import Session, select

from newboom.boom_compute import _today_local, process_daily_summary_pipeline
from newboom.database import engine
from newboom.model import (
    PPI,
    ActivityState,
    DailySummary,
    Device,
    Progress,
    Task,
    WindowMetric,
    WindowState,
)


def _night_windows(session: Session, device_id: int, night: date, count: int) -> None:
    """Seed ``count`` 5-min windows for a night so a summary can be computed."""
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
                rmssd_ms=40.0,
                ln_rmssd=float(np.log(40.0)),
                state=WindowState.PARASYMPATHETIC,
                is_sleep=True,
                fresh=False,
            )
        )


def _make_device(session: Session, mac: str) -> int:
    dev = Device(name="pipeline", mac=mac, tz="08:00", age=30)
    session.add(dev)
    session.commit()
    session.refresh(dev)
    return int(dev.id)


def test_open_day_recomputed_when_cursor_at_today():
    with Session(engine) as s:
        device_id = _make_device(s, "00:11:22:33:44:55")
        device = s.exec(select(Device).where(Device.id == device_id)).first()
        today = _today_local(device)
        midnight_today = datetime(today.year, today.month, today.day)

        # The device is streaming the open day: raw PPI + night windows exist.
        s.add(
            PPI(
                device_id=device_id,
                time=datetime(today.year, today.month, today.day, 9, 0),
                hr=70,
                spo2=950,
                ppi=700,
                activity=ActivityState.RELAXED,
            )
        )
        _night_windows(s, device_id, today, 30)
        # Poisoned state the pre-fix code produced at local 00:00: the cursor
        # already jumped to today with no summary yet.
        s.add(
            Progress(
                device_id=device_id, task=Task.DAILY_SUMMARY, end_time=midnight_today
            )
        )
        s.commit()

        assert (
            s.exec(
                select(DailySummary)
                .where(DailySummary.device_id == device_id)
                .where(DailySummary.summary_date == today)
            ).first()
            is None
        )

    # The pipeline must (re)compute the open day even with the cursor at today.
    process_daily_summary_pipeline(device_id)

    with Session(engine) as s:
        summary = s.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device_id)
            .where(DailySummary.summary_date == today)
        ).first()
        assert (
            summary is not None
        ), "open day must be recomputed even when the cursor sits at today"
        cursor = s.exec(
            select(Progress)
            .where(Progress.device_id == device_id)
            .where(Progress.task == Task.DAILY_SUMMARY)
        ).first()
        # The cursor must NOT be advanced past the open day; that advance is
        # exactly what froze the day forever.
        assert cursor.end_time.date() == today


def test_closed_days_processed_and_cursor_advances():
    with Session(engine) as s:
        device_id = _make_device(s, "00:11:22:33:44:66")
        device = s.exec(select(Device).where(Device.id == device_id)).first()
        today = _today_local(device)
        yesterday = today - timedelta(days=1)

        # Raw PPI so the pipeline forms a baseline start, plus a closed day
        # (yesterday) with windows and a cursor parked before it.
        s.add(
            PPI(
                device_id=device_id,
                time=datetime(yesterday.year, yesterday.month, yesterday.day, 9, 0),
                hr=70,
                spo2=950,
                ppi=700,
                activity=ActivityState.RELAXED,
            )
        )
        _night_windows(s, device_id, yesterday, 30)
        s.add(
            Progress(
                device_id=device_id,
                task=Task.DAILY_SUMMARY,
                end_time=datetime(
                    yesterday.year, yesterday.month, yesterday.day
                )
                - timedelta(days=1),
            )
        )
        s.commit()

    process_daily_summary_pipeline(device_id)

    with Session(engine) as s:
        summary = s.exec(
            select(DailySummary)
            .where(DailySummary.device_id == device_id)
            .where(DailySummary.summary_date == yesterday)
        ).first()
        assert summary is not None, "a closed day with data must get a summary"
        cursor = s.exec(
            select(Progress)
            .where(Progress.device_id == device_id)
            .where(Progress.task == Task.DAILY_SUMMARY)
        ).first()
        # A closed day the cursor advances past (but not past the open day).
        assert cursor.end_time.date() == yesterday

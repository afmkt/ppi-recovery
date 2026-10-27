"""P1-1 / P1-3 / P1-4: autonomic episodes, night-anchored windows, clean PPI.

- P1-1: autonomic state-episode structure (per-night, state-change split).
- P1-3: window_mid_h is hours from the night's local start, not the first PPI.
- P1-4: clean_ppi prepends ~6-point left context across batch boundaries.
"""
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from newboom.compute import (
    build_autonomic_episodes,
    clean_ppi,
    clean_ppi_series,
    process_autonomic_episodes,
    window_metrics_for_device,
)
from newboom.database import engine
from newboom.model import (
    ActivityState,
    AutonomicEpisode,
    Device,
    PPI,
    WindowMetric,
    WindowState,
)


# --- P1-1 autonomic episodes ------------------------------------------------


def test_build_episodes_split_on_state_change():
    night = date(2026, 10, 27)
    night_start = datetime(night.year, night.month, night.day, 8, 0)
    windows: list[WindowMetric] = []
    with Session(engine) as s:
        dev = Device(name="t", mac="00:00:00:00:00:08", age=30)
        s.add(dev)
        s.commit()
        for i in range(6):
            w = WindowMetric(
                device_id=dev.id,
                night_date=night,
                window_start=night_start + timedelta(minutes=i),
                window_end=night_start + timedelta(minutes=i + 5),
                window_mid_h=i / 60.0,
                valid_ratio=1.0,
                state=WindowState.SYMPATHETIC if i < 3 else WindowState.PARASYMPATHETIC,
                is_sleep=True,
                lf_hf=3.0 if i < 3 else 1.0,
            )
            s.add(w)
            windows.append(w)
        s.commit()
        eps = build_autonomic_episodes(night, windows)
    assert [e.state for e in eps] == [WindowState.SYMPATHETIC, WindowState.PARASYMPATHETIC]
    assert eps[0].window_count == 3
    assert eps[1].window_count == 3
    assert eps[0].duration_min == 3.0  # window_count * 1-min step


def test_process_autonomic_episodes_persists():
    with Session(engine) as s:
        dev = Device(name="t", mac="00:00:00:00:00:09", age=30)
        s.add(dev)
        s.commit()
        device_id = int(dev.id)
        night = date(2026, 10, 27)
        night_start = datetime(night.year, night.month, night.day, 8, 0)
        for i in range(6):
            s.add(
                WindowMetric(
                    device_id=dev.id,
                    night_date=night,
                    window_start=night_start + timedelta(minutes=i),
                    window_end=night_start + timedelta(minutes=i + 5),
                    window_mid_h=i / 60.0,
                    valid_ratio=1.0,
                    state=WindowState.SYMPATHETIC if i < 3 else WindowState.PARASYMPATHETIC,
                    is_sleep=True,
                    fresh=True,
                )
            )
        s.commit()

    count = process_autonomic_episodes(device_id)
    assert count == 2
    with Session(engine) as s:
        eps = s.exec(
            select(AutonomicEpisode)
            .where(AutonomicEpisode.device_id == device_id)
            .order_by(AutonomicEpisode.episode_id)
        ).all()
        assert [e.state for e in eps] == [WindowState.SYMPATHETIC, WindowState.PARASYMPATHETIC]
        assert eps[0].window_count == 3
        assert eps[1].window_count == 3


# --- P1-3 night-anchored window mid ----------------------------------------


def test_window_mid_h_is_night_anchored():
    # PPI starts 5 min AFTER the night start; the first window's mid must be
    # measured from 08:00 (7.5 min -> 0.125h), not from the first PPI (08:05).
    with Session(engine) as s:
        dev = Device(name="t", mac="00:00:00:00:00:0A", age=30)
        s.add(dev)
        s.commit()
        device_id = int(dev.id)
        base = datetime(2026, 10, 27, 8, 5)
        for i in range(4):
            s.add(
                PPI(
                    device_id=dev.id,
                    time=base + timedelta(minutes=5 * i),
                    hr=60,
                    spo2=98,
                    ppi=1000,
                    ppi_clean=1000.0,
                    activity=ActivityState.DEEP_SLEEP,
                    fresh=False,
                )
            )
        s.commit()

    metrics = window_metrics_for_device(
        device_id,
        datetime(2026, 10, 27, 8, 0),
        datetime(2026, 10, 28, 8, 0),
    )
    assert metrics
    first = min(metrics, key=lambda m: m.window_start)
    assert abs(first.window_mid_h - 0.125) < 0.01


# --- P1-4 clean PPI context -------------------------------------------------


def test_clean_series_context_shifts_boundary():
    # A lone 1500ms PPI is "valid" when its +-5 window is all 1500 (median 1500),
    # but "invalid" once 12 x 1000ms of left context are prepended: the +-5
    # window is then dominated by 1000, so the local median is 1000 and
    # |1500 - 1000| = 500 exceeds the max(250, 0.25*1000)=250 tolerance.
    assert clean_ppi_series([1500.0] * 6)[0] == 1500.0
    assert clean_ppi_series([1000.0] * 12 + [1500.0])[12] is None


def test_clean_ppi_prepends_context_only_writes_fresh():
    with Session(engine) as s:
        dev = Device(name="t", mac="00:00:00:00:00:0B", age=30)
        s.add(dev)
        s.commit()
        device_id = int(dev.id)
        for i in range(6):
            s.add(
                PPI(
                    device_id=dev.id,
                    time=datetime(2026, 10, 27, 8, i),
                    hr=60,
                    spo2=98,
                    ppi=1000,
                    ppi_clean=1000.0,
                    activity=ActivityState.DEEP_SLEEP,
                    fresh=False,
                )
            )
        for i in range(3):
            s.add(
                PPI(
                    device_id=dev.id,
                    time=datetime(2026, 10, 27, 9, i),
                    hr=60,
                    spo2=98,
                    ppi=1000,
                    ppi_clean=None,
                    activity=ActivityState.DEEP_SLEEP,
                    fresh=True,
                )
            )
        s.commit()
        clean_ppi([dev])

    with Session(engine) as s:
        ctx = s.exec(
            select(PPI).where(PPI.device_id == device_id).where(PPI.time < datetime(2026, 10, 27, 9, 0))
        ).all()
        assert all(p.fresh is False and p.ppi_clean == 1000.0 for p in ctx)
        fresh = s.exec(
            select(PPI).where(PPI.device_id == device_id).where(PPI.time >= datetime(2026, 10, 27, 9, 0))
        ).all()
        # the fresh batch was cleaned and un-flagged
        assert all(p.fresh is False and p.ppi_clean == 1000.0 for p in fresh)

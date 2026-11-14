"""Regression: morning start energy E_d recovers off the last valid day-end
F_d (spec §8), not a full-tank FIRST_ENERGY.

Before the fix, the per-minute energy path fell back to ``FIRST_ENERGY`` (1.0)
for the first minutes of a night whose ``DailySummary`` was not yet written, so
the Load & Energy chart showed a false 0→100 spike at every night boundary
(``total_energy`` jumped from the prior night's floored 0 to a full-tank 100,
then settled to the true recovered value minutes later). ``recover_start_energy``
is now the single source of E_d, called both by the summary stage (stored on
``energy_start_e``) and on the fly by the minute path, single-step off the most
recent valid ``F_d`` within ``STALE_WINDOW_DAYS``.
"""

from datetime import date, datetime
import math

import pytest
from sqlmodel import Session, select

from newboom.boom_compute import process_minute_metric, recover_start_energy
from newboom.config import FIRST_ENERGY, TAU_BASE
from newboom.database import engine
from newboom.model import (
    ActivityState,
    DailySummary,
    Device,
    MinuteMetric,
    PPI,
)


def _make_device(session: Session, mac: str) -> int:
    dev = Device(name="energy", mac=mac, tz="08:00", age=30)
    session.add(dev)
    session.commit()
    session.refresh(dev)
    return int(dev.id)


def _seed_summary(session: Session, device_id: int, d: date, f_d: float | None) -> None:
    session.add(DailySummary(device_id=device_id, summary_date=d, energy_end_f=f_d))
    session.commit()


def _expected_off(f_d: float, tau: float = TAU_BASE) -> float:
    """The single-step recovery E_d = clamp(1 - (1 - F_d) * exp(-1/tau), 0, 1)."""
    return max(0.0, min(1.0, 1.0 - (1.0 - f_d) * math.exp(-1.0 / tau)))


# --- recover_start_energy ---------------------------------------------------


def test_recover_no_valid_f_d_is_first_energy():
    # No summary at all -> first enablement: E_1 = FIRST_ENERGY.
    with Session(engine) as s:
        did = _make_device(s, "ER:00:00:00:00:01")
        assert recover_start_energy(s, did, date(2026, 11, 14)) == FIRST_ENERGY


def test_recover_single_step_off_last_valid_f_d():
    # One valid F_d within the window recovers one step off it -- NOT a full tank.
    with Session(engine) as s:
        did = _make_device(s, "ER:00:00:00:00:02")
        _seed_summary(s, did, date(2026, 11, 13), f_d=0.2)
        e = recover_start_energy(s, did, date(2026, 11, 14))
        assert e == pytest.approx(_expected_off(0.2))
        assert e < FIRST_ENERGY  # recovered, not a full tank
        assert 0.5 < e < 0.7


def test_recover_seven_day_gap_is_within_window():
    # Exactly STALE_WINDOW_DAYS back still recovers (gap <= window).
    with Session(engine) as s:
        did = _make_device(s, "ER:00:00:00:00:03")
        _seed_summary(s, did, date(2026, 11, 6), f_d=0.2)  # 7 days before target
        e = recover_start_energy(s, did, date(2026, 11, 13))
        assert e == pytest.approx(_expected_off(0.2))
        assert e < FIRST_ENERGY


def test_recover_stale_gap_beyond_window_falls_back():
    # Last valid F_d 8 days back (> STALE_WINDOW_DAYS) -> untrustworthy -> E_1.
    with Session(engine) as s:
        did = _make_device(s, "ER:00:00:00:00:04")
        _seed_summary(s, did, date(2026, 11, 4), f_d=0.2)  # 9 days before target
        assert (
            recover_start_energy(s, did, date(2026, 11, 13)) == FIRST_ENERGY
        )


def test_incomplete_day_is_skipped_to_last_valid_f_d():
    # A day with no F_d (incomplete) is excluded; recovery chains off the last
    # valid F_d, not the incomplete one.
    with Session(engine) as s:
        did = _make_device(s, "ER:00:00:00:00:05")
        _seed_summary(s, did, date(2026, 11, 13), f_d=None)  # incomplete: no F_d
        _seed_summary(s, did, date(2026, 11, 12), f_d=0.3)  # last valid
        e = recover_start_energy(s, did, date(2026, 11, 14))
        assert e == pytest.approx(_expected_off(0.3))
        assert e < FIRST_ENERGY


# --- on-the-fly minute path -------------------------------------------------


def test_minute_metric_recovers_on_the_fly_for_summaryless_night():
    # The exact bug: a night with PPI but no DailySummary yet. The first minutes
    # must read the recovered E_d off the prior F_d -- NOT a full-tank 100.
    with Session(engine) as s:
        did = _make_device(s, "ER:00:00:00:00:06")
        _seed_summary(s, did, date(2026, 11, 13), f_d=0.2)  # prior valid F_d
        # NO summary for 2026-11-14. Resting PPI at the start of the night:
        # hr=50 < DEFAULT_HR_REST=60 -> zero load -> total_energy = round(100*E_d).
        for m in range(6):
            s.add(
                PPI(
                    device_id=did,
                    time=datetime(2026, 11, 14, 8, m, 0),
                    hr=50,
                    spo2=950,
                    ppi=700,
                    activity=ActivityState.RELAXED,
                )
            )
        s.commit()
        expected_e = recover_start_energy(s, did, date(2026, 11, 14))

    assert expected_e < FIRST_ENERGY  # sanity: a real recovery, not a full tank

    process_minute_metric(did)

    with Session(engine) as s:
        first = s.exec(
            select(MinuteMetric)
            .where(MinuteMetric.device_id == did)
            .where(MinuteMetric.night_date == date(2026, 11, 14))
            .order_by(MinuteMetric.timestamp.asc())
            .limit(1)
        ).first()
        assert first is not None, "night must produce minute metrics"
        # The regression: it must NOT be a full tank.
        assert first.total_energy != 100
        # And it must equal the recovered value the helper produces.
        assert first.total_energy == round(100 * expected_e)


def test_minute_metric_first_night_defaults_to_first_energy():
    # No prior F_d anywhere: the very first night recovers to E_1 = 100 (correct).
    with Session(engine) as s:
        did = _make_device(s, "ER:00:00:00:00:07")
        # no summaries at all -> first enablement
        for m in range(3):
            s.add(
                PPI(
                    device_id=did,
                    time=datetime(2026, 11, 5, 8, m, 0),
                    hr=50,
                    spo2=950,
                    ppi=700,
                    activity=ActivityState.RELAXED,
                )
            )
        s.commit()

    process_minute_metric(did)

    with Session(engine) as s:
        first = s.exec(
            select(MinuteMetric)
            .where(MinuteMetric.device_id == did)
            .order_by(MinuteMetric.timestamp.asc())
            .limit(1)
        ).first()
        assert first is not None
        assert first.total_energy == 100  # E_1 = FIRST_ENERGY, the true first day

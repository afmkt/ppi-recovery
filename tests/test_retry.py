"""Regression tests for the transient-lock retry primitives in
``newboom.database`` (``is_transient_lock`` / ``retry`` / ``run_write``).

A SQLite commit is atomic, so the contract under test is: only transient
lock/busy errors are retried (never permanent ones), a transient failure is
retried to success, and an idempotent upsert lands exactly once even across a
simulated lock.
"""

import sqlite3
from datetime import datetime

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from newboom.database import engine, is_transient_lock, retry, run_write
from newboom.model import PPI, Device


def _op(msg: str) -> OperationalError:
    """Build a SQLAlchemy OperationalError wrapping a sqlite3 message."""
    return OperationalError("stmt", {}, sqlite3.OperationalError(msg))


def _upsert(s: Session, device_id: int, t: datetime, hr: int, spo2: int, ppi: float, activity: int) -> None:
    s.execute(
        insert(PPI)
        .values(time=t, hr=hr, spo2=spo2, ppi=ppi, activity=activity, device_id=device_id)
        .on_conflict_do_update(
            index_elements=["device_id", "time"],
            set_={"hr": hr, "spo2": spo2, "ppi": ppi, "activity": activity, "fresh": True},
        )
    )


def test_is_transient_lock_only_for_contention() -> None:
    assert is_transient_lock(_op("database is locked")) is True
    assert is_transient_lock(_op("database is busy")) is True
    # Permanent errors must NOT be retried: retrying would loop and mask the bug.
    assert is_transient_lock(_op("database is corrupt")) is False
    assert is_transient_lock(_op("no such table: foo")) is False
    assert is_transient_lock(ValueError("x")) is False


def test_retry_recovers_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _op("database is locked")
        return "ok"

    assert retry(flaky, base_delay=0.001, max_delay=0.002) == "ok"
    assert calls["n"] == 3


def test_retry_does_not_mask_permanent_errors() -> None:
    calls = {"n": 0}

    def bad() -> None:
        calls["n"] += 1
        raise _op("database is corrupt")

    try:
        retry(bad, base_delay=0.001)
        raise AssertionError("a permanent error must propagate")
    except OperationalError:
        assert calls["n"] == 1, "a permanent error must not be retried"


def test_run_write_retries_and_upsert_is_idempotent() -> None:
    with Session(engine) as s:
        d = Device(name="dev", mac="AA:BB:CC:DD:EE:FF", tz="UTC")
        s.add(d)
        s.commit()
        s.refresh(d)
        dev_id = d.id

    # A transient lock on attempt 1, then a real commit: the unit must retry once.
    state = {"n": 0}

    def upsert_then_succeed(s: Session) -> None:
        state["n"] += 1
        if state["n"] == 1:
            raise _op("database is locked")
        _upsert(s, dev_id, datetime(2026, 1, 1), 60, 100, 50.0, 3)
        s.commit()

    run_write(Session(engine), upsert_then_succeed, base_delay=0.001, max_delay=0.002)
    assert state["n"] == 2, "the transient lock must have been retried once"

    # Three more upserts of the same (device_id, time): still exactly one row.
    for _ in range(3):
        with Session(engine) as s:
            _upsert(s, dev_id, datetime(2026, 1, 2), 70, 99, 40.0, 4)
            s.commit()

    with Session(engine) as s:
        assert len(s.exec(select(PPI)).all()) == 2, "upserts must be idempotent"

"""§1.1 /go, §2.1 /status, §5.1 /load_status resting-HR display fallback.

Regression for "没测出睡眠 / 静息心率也没有": the per-day
``rest_heart_rate_val`` is only written on days that have a sleep record
(``boom_compute``: ``rest_hr_val = sleep_record.heart_rate_rest if sleep_record
else None``), so when the device has not slept since, the *latest* day's
``rest_heart_rate_val`` is null and the old code showed ``0`` — the spec's
forbidden "用0替代" — even though a 7-day baseline (``rest_heart_rate_avg``)
exists. All three "current card" endpoints must now fall back to that baseline
(:func:`newboom.boom.staleness.current_resting_hr`), matching what
``/body_metrics`` already shows, instead of a stale ``0``.
"""

from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import Session

from newboom.api import app
from newboom.boom.staleness import current_resting_hr
from newboom.database import engine
from newboom.model import DailySummary, Device

client = TestClient(app)  # bare client: no lifespan, so the worker loop never starts


def _seed_device(mac: str) -> int:
    with Session(engine) as s:
        d = Device(name="rhr", mac=mac, age=30)
        s.add(d)
        s.commit()
        s.refresh(d)
        return int(d.id)


def _seed_summary(device_id: int, val: float | None, avg: float | None) -> None:
    """One latest-day DailySummary carrying the given resting-HR fields."""
    with Session(engine) as s:
        s.add(
            DailySummary(
                device_id=device_id,
                summary_date=date(2026, 11, 19),
                rest_heart_rate_val=val,
                rest_heart_rate_avg=avg,
            )
        )
        s.commit()


def _hr_via_go(mac: str) -> int:
    r = client.get(f"/devices/{mac}/go")
    assert r.status_code == 200
    return r.json()["data"]["healthCardValues"]["restingHeartRate"]


def _hr_via_status(mac: str) -> int:
    r = client.get(f"/devices/{mac}/status")
    assert r.status_code == 200
    return r.json()["data"]["restingHeartRate"]


def _hr_via_load(mac: str) -> int:
    r = client.get(f"/devices/{mac}/load_status")
    assert r.status_code == 200
    return r.json()["data"]["restingHeartRate"]


# --- the reported bug: no recent sleep, but a baseline exists ---------------


def test_val_null_falls_back_to_baseline_on_all_three_cards():
    # Latest day has no sleep-derived val (no sleep since) but a 7-day baseline.
    mac = "LS:00:00:00:01:01"
    did = _seed_device(mac)
    _seed_summary(did, val=None, avg=63.0)
    assert _hr_via_go(mac) == 63
    assert _hr_via_status(mac) == 63
    assert _hr_via_load(mac) == 63


# --- val present wins over the baseline ------------------------------------


def test_val_present_shows_val_not_baseline():
    mac = "LS:00:00:00:01:02"
    did = _seed_device(mac)
    _seed_summary(did, val=58.0, avg=63.0)
    assert _hr_via_go(mac) == 58
    assert _hr_via_status(mac) == 58
    assert _hr_via_load(mac) == 58


# --- no data at all: the 0 sentinel, not a fabricated value ----------------


def test_both_null_shows_zero_sentinel():
    mac = "LS:00:00:00:01:03"
    did = _seed_device(mac)
    _seed_summary(did, val=None, avg=None)
    assert _hr_via_go(mac) == 0
    assert _hr_via_status(mac) == 0
    assert _hr_via_load(mac) == 0


# --- the pure helper: val -> avg -> 0 --------------------------------------


def test_current_resting_hr_prefers_val_then_avg_then_zero():
    assert current_resting_hr(_row(58.4, 63.0)) == 58  # val, rounded
    assert current_resting_hr(_row(None, 63.0)) == 63  # baseline when val null
    assert current_resting_hr(_row(None, None)) == 0  # no data -> sentinel


def _row(val: float | None, avg: float | None) -> DailySummary:
    return DailySummary(rest_heart_rate_val=val, rest_heart_rate_avg=avg)

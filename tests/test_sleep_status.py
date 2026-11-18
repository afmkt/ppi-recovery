"""§3.1 sleep_status: a night's total sleep must be its main overnight record.

Regression for the "sleepDuration too short" report: the device uploads several
``Sleep`` rows that ``night_key`` folds into one date — a main overnight plus a
daytime nap (or a partial capture) that settles later. Selecting the *latest*
row shadowed the real night with the nap, so the card showed 1h07m while
quality/recovery (from the main night) showed 8h+. The night's record is now the
longest-total-sleep one, so the card is consistent with the daily summary.
"""

from datetime import date, datetime

from fastapi.testclient import TestClient
from sqlmodel import Session

from newboom.api import app
from newboom.boom.sleep import _night_sleep_records, _sleep_window
from newboom.database import engine
from newboom.model import DailySummary, Device, Sleep

client = TestClient(app)  # bare client: no lifespan, so the worker loop never starts

# A night with two records: a main overnight that settles at 10:00, then a short
# nap that settles at 12:00. Both fold to the same night_date (hour >= 8), so the
# latest-by-time selection would wrongly pick the 1h nap.
MAIN = {
    "time": datetime(2026, 11, 18, 10, 0),
    "sleep_onset_time": 9 * 3600,  # bed 01:00
    "awake_time": 0,  # wake 10:00
    "light_sleep_period": 4 * 3600,
    "deep_sleep_period": 4 * 3600,
    "other_sleep_period": 0,  # total 8h0m
}
NAP = {
    "time": datetime(2026, 11, 18, 12, 0),
    "sleep_onset_time": 3600,  # bed 11:00
    "awake_time": 0,  # wake 12:00
    "light_sleep_period": 3600,
    "deep_sleep_period": 0,
    "other_sleep_period": 0,  # total 1h0m
}


def _seed_device(mac: str) -> int:
    with Session(engine) as s:
        d = Device(name="sl", mac=mac, age=30)
        s.add(d)
        s.commit()
        s.refresh(d)
        return int(d.id)


def _seed_sleep(device_id: int, **kw) -> None:
    with Session(engine) as s:
        s.add(
            Sleep(device_id=device_id, timezone="08:00", heart_rate_rest=55, **kw)
        )
        s.commit()


def _seed_summary(device_id: int, d: date) -> None:
    with Session(engine) as s:
        s.add(
            DailySummary(
                device_id=device_id,
                summary_date=d,
                sleep_quality=100,
                recovery_sleep_percentage=55,
                recovery_sleep_duration_min=4 * 60,
            )
        )
        s.commit()


def test_sleep_status_picks_main_night_over_later_nap():
    did = _seed_device("SL:00:00:00:00:01")
    _seed_sleep(did, **MAIN)
    _seed_sleep(did, **NAP)
    _seed_summary(did, date(2026, 11, 18))
    r = client.get(
        "/devices/SL:00:00:00:00:01/sleep_status", params={"date": "2026-11-18"}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    # The card shows the 8h main night, not the 1h nap — and stays consistent
    # with sleepQuality=100 / recoverySleepDuration that come from the main
    # night.
    assert data["sleepDuration"] == "8小时0分钟"
    assert data["sleepDuration"] != "1小时0分钟"
    assert data["sleepQuality"] == 100
    assert data["recoverySleepDuration"] == "4小时0分"


def test_night_sleep_records_picks_longest_per_night():
    did = _seed_device("SL:00:00:00:00:02")
    _seed_sleep(did, **MAIN)
    _seed_sleep(did, **NAP)
    with Session(engine) as s:
        recs = _night_sleep_records(s, did, date(2026, 11, 12), date(2026, 11, 18))
    # The main night (8h), not the 1h nap.
    assert recs[date(2026, 11, 18)].total_sleep_seconds == 8 * 3600


def test_sleep_window_uses_longest_sleep_record():
    did = _seed_device("SL:00:00:00:00:03")
    _seed_sleep(did, **MAIN)
    _seed_sleep(did, **NAP)
    with Session(engine) as s:
        bed, wake = _sleep_window(s, did, date(2026, 11, 18))
    # The main night's window (01:00 -> 10:00), not the nap's (11:00 -> 12:00).
    assert (bed.hour, bed.minute) == (1, 0)
    assert (wake.hour, wake.minute) == (10, 0)

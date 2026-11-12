"""§5.1 /load_status totalLoad contract.

Regression for the "totalLoad stuck at 65" report: the card must show the
day's *actual* accumulated total load (spec §7: totalLoad = S1 + tss), not
``load_score`` — a status/recovery percentage that is a fixed 65 in the
first-7-days temporary mode (``STATUS_LOAD_DEFAULT``). For the current,
in-progress day the finalized total is not written yet, so totalLoad falls
back to the last minute's cumulative load — the live value the realtime
curve ends on.
"""

from datetime import date, datetime

from fastapi.testclient import TestClient
from sqlmodel import Session

from newboom.api import app
from newboom.database import engine
from newboom.model import DailySummary, Device, MinuteMetric

client = TestClient(app)  # bare client: no lifespan, so the worker loop never starts


def _seed_device(mac: str) -> int:
    with Session(engine) as s:
        d = Device(name="ls", mac=mac, age=30)
        s.add(d)
        s.commit()
        s.refresh(d)
        return int(d.id)


def _seed_summary(
    device_id: int, d: date, load_score: int, total_load: float | None
) -> None:
    with Session(engine) as s:
        s.add(
            DailySummary(
                device_id=device_id,
                summary_date=d,
                load_score=load_score,
                total_load=total_load,
            )
        )
        s.commit()


def _seed_minute(
    device_id: int, ts: datetime, night: date, total_load: float, hr: float = 60.0
) -> None:
    with Session(engine) as s:
        s.add(
            MinuteMetric(
                device_id=device_id,
                timestamp=ts,
                night_date=night,
                heart_rate=hr,
                total_load=total_load,
                s1_load=total_load,
                tss_load=0.0,
                load_delta=0.0,
            )
        )
        s.commit()


def test_total_load_uses_actual_load_not_load_score_on_finalized_day():
    # Temp mode: load_score is the fixed 65 default; the day's real total is 150.
    did = _seed_device("LS:00:00:00:00:01")
    d = date(2026, 11, 11)
    _seed_summary(did, d, load_score=65, total_load=150.4)
    _seed_minute(did, datetime(2026, 11, 11, 8, 0), d, total_load=150.4)
    r = client.get(
        "/devices/LS:00:00:00:00:01/load_status", params={"date": "2026-11-11"}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["totalLoad"] == 150  # the actual total load, not 65
    assert data["totalLoad"] != 65
    assert data["loadProgress"] == 150  # same value, as before


def test_total_load_falls_back_to_last_minute_for_in_progress_day():
    # Current day: total_load not finalized (None) -> last minute's cumulative load.
    did = _seed_device("LS:00:00:00:00:02")
    d = date(2026, 11, 12)
    _seed_summary(did, d, load_score=65, total_load=None)
    _seed_minute(did, datetime(2026, 11, 12, 10, 36), d, total_load=108.0)
    _seed_minute(did, datetime(2026, 11, 12, 11, 19), d, total_load=127.18)
    r = client.get("/devices/LS:00:00:00:00:02/load_status")  # no date -> latest day
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["totalLoad"] == 127  # round(127.18), the live value
    assert data["loadProgress"] == 127
    assert data["totalLoad"] != 65
    # Consistency: the realtime curve ends on the same value the card shows.
    assert data["realtimeLoadData"][-1]["value"] == 127


def test_total_load_matches_load_trend_for_finalized_day():
    # /load_status totalLoad and /load_trend's day value must agree (both the
    # day's finalized total_load), so the headline never disagrees with the trend.
    did = _seed_device("LS:00:00:00:00:03")
    d = date(2026, 11, 11)
    _seed_summary(did, d, load_score=65, total_load=150.4)
    _seed_minute(did, datetime(2026, 11, 11, 8, 0), d, total_load=150.4)
    st = client.get(
        "/devices/LS:00:00:00:00:03/load_status", params={"date": "2026-11-11"}
    )
    tr = client.get(
        "/devices/LS:00:00:00:00:03/load_trend",
        params={"startDate": "2026-11-11", "endDate": "2026-11-11"},
    )
    assert st.json()["data"]["totalLoad"] == tr.json()["data"][0]["value"] == 150


def test_total_load_zero_when_no_summary():
    _seed_device("LS:00:00:00:00:04")  # device exists, no summary at all
    r = client.get("/devices/LS:00:00:00:00:04/load_status")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["totalLoad"] == 0
    assert data["asOf"] is None
    assert data["stale"] is True


# --- §1.1 /go consistency: healthStatus.load == §5.1 totalLoad --------------


def test_go_load_matches_load_status_total_load_finalized():
    # §1.1 /go healthStatus.load and §5.1 /load_status totalLoad must be the
    # same value: the day's actual accumulated total load, NOT load_score (65).
    did = _seed_device("LS:00:00:00:00:05")
    d = date(2026, 11, 11)
    _seed_summary(did, d, load_score=65, total_load=150.4)
    _seed_minute(did, datetime(2026, 11, 11, 8, 0), d, total_load=150.4)

    go = client.get("/devices/LS:00:00:00:00:05/go")
    ls = client.get(
        "/devices/LS:00:00:00:00:05/load_status", params={"date": "2026-11-11"}
    )
    assert go.status_code == 200
    assert ls.status_code == 200
    go_load = go.json()["data"]["healthStatus"]["load"]
    ls_load = ls.json()["data"]["totalLoad"]
    assert go_load == ls_load == 150
    assert go_load != 65  # the "stuck at 65" bug: load_score is not the total


def test_go_load_matches_load_status_total_load_in_progress():
    # Current, in-progress day: total_load not finalized -> both fall back to the
    # last minute's cumulative load (the live value the curve ends on), not 65.
    did = _seed_device("LS:00:00:00:00:06")
    d = date(2026, 11, 12)
    _seed_summary(did, d, load_score=65, total_load=None)
    _seed_minute(did, datetime(2026, 11, 12, 10, 36), d, total_load=108.0)
    _seed_minute(did, datetime(2026, 11, 12, 11, 19), d, total_load=127.18)

    go = client.get("/devices/LS:00:00:00:00:06/go")
    ls = client.get("/devices/LS:00:00:00:00:06/load_status")  # no date -> latest day
    assert go.status_code == 200
    assert ls.status_code == 200
    go_load = go.json()["data"]["healthStatus"]["load"]
    ls_load = ls.json()["data"]["totalLoad"]
    assert go_load == ls_load == 127
    assert go_load != 65

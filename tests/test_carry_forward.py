"""Carry-forward / staleness strategy for the BOOM read surface.

Pins the production-data-degradation contract:
  * current cards mark a value ``stale`` when its source day is older than the
    window (or there is no data at all) but still return the value;
  * trends LOCF-fill a missing day with the last real value across a gap <= the
    window, marked ``carried``; a larger gap is left as a real hole (never
    fabricated, never zeroed).

Pure helpers are tested directly; the ``/go`` and ``/readiness`` endpoints are
exercised through the real app to lock the response contract.
"""

from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session

from newboom.api import app
from newboom.boom.schemas import AtlCtlPoint, DatePoint
from newboom.boom.staleness import local_today, locf, locf_atlctl, mark_stale
from newboom.database import engine
from newboom.model import DailySummary, Device, Sleep

client = TestClient(app)  # bare client: no lifespan, so the worker loop never starts


# --- mark_stale (current cards) --------------------------------------------


def test_mark_stale_no_data_is_none_and_stale():
    as_of, stale = mark_stale(None, now=date(2026, 10, 10))
    assert as_of is None
    assert stale is True


def test_mark_stale_within_window_is_fresh():
    as_of, stale = mark_stale(date(2026, 10, 5), now=date(2026, 10, 10))
    assert as_of == "2026-10-05"
    assert stale is False


def test_mark_stale_seven_days_is_fresh_boundary():
    # "within 7 days" -> <= 7 days is fresh; 7 > 7 is False.
    as_of, stale = mark_stale(date(2026, 10, 3), now=date(2026, 10, 10))
    assert as_of == "2026-10-03"
    assert stale is False


def test_mark_stale_eight_days_is_stale():
    as_of, stale = mark_stale(date(2026, 10, 2), now=date(2026, 10, 10))
    assert as_of == "2026-10-02"
    assert stale is True


def test_local_today_none_device_is_wall_clock():
    import datetime as _dt

    assert local_today(None) == _dt.date.today()


# --- locf (trends) ---------------------------------------------------------


def _dp(d: str, v: float, carried: bool = False) -> DatePoint:
    return DatePoint(date=d, value=v, carried=carried)


def test_locf_carries_within_window_marked():
    # real on 10-01 and 10-05; 10-02..10-04 are a 3-day gap (<= 7) -> carried.
    real = [_dp("2026-10-01", 10.0), _dp("2026-10-05", 50.0)]
    out = locf(real, date(2026, 10, 1), date(2026, 10, 5))
    by_date = {p.date: p for p in out}
    assert by_date["2026-10-01"].carried is False
    assert by_date["2026-10-01"].value == 10.0
    assert by_date["2026-10-05"].carried is False
    assert by_date["2026-10-05"].value == 50.0
    for d in ("2026-10-02", "2026-10-03", "2026-10-04"):
        assert by_date[d].carried is True
        assert by_date[d].value == 10.0  # carried from the last real (10-01)


def test_locf_omits_gap_wider_than_window():
    # 10-01 and 10-15: a 14-day gap. 10-02..10-08 carry from 10-01 (<= 7);
    # 10-09..10-14 are > 7 from any real value -> left as a real hole.
    real = [_dp("2026-10-01", 10.0), _dp("2026-10-15", 90.0)]
    out = locf(real, date(2026, 10, 1), date(2026, 10, 15))
    by_date = {p.date: p for p in out}
    assert by_date["2026-10-01"].carried is False
    assert by_date["2026-10-15"].carried is False
    assert by_date["2026-10-08"].carried is True
    assert by_date["2026-10-08"].value == 10.0
    assert "2026-10-12" not in by_date  # deep in the wide gap: a real hole


def test_locf_leading_seed_from_before_start():
    # A real value just before `start` seeds the early days of the range.
    real = [_dp("2026-09-28", 30.0)]
    out = locf(real, date(2026, 9, 30), date(2026, 10, 2))
    by_date = {p.date: p for p in out}
    assert by_date["2026-09-30"].carried is True
    assert by_date["2026-09-30"].value == 30.0
    assert by_date["2026-10-02"].carried is True
    assert by_date["2026-10-02"].value == 30.0


def test_locf_no_real_data_is_empty():
    assert locf([], date(2026, 10, 1), date(2026, 10, 5)) == []


def test_locf_atlctl_carries_pair_together():
    real = [
        AtlCtlPoint(date="2026-10-01", atl=40, ctl=60),
        AtlCtlPoint(date="2026-10-05", atl=80, ctl=70),
    ]
    out = locf_atlctl(real, date(2026, 10, 1), date(2026, 10, 5))
    by_date = {p.date: p for p in out}
    assert by_date["2026-10-03"].carried is True
    assert by_date["2026-10-03"].atl == 40
    assert by_date["2026-10-03"].ctl == 60
    assert by_date["2026-10-01"].carried is False


# --- endpoint contract (real app) ------------------------------------------


def _seed_summary(device_id: int, d: date, status_score: int = 50) -> None:
    with Session(engine) as s:
        s.add(
            DailySummary(device_id=device_id, summary_date=d, status_score=status_score)
        )
        s.commit()


def _seed_device(mac: str, tz: str | None = None) -> int:
    with Session(engine) as s:
        d = Device(name="cf", mac=mac, tz=tz)
        s.add(d)
        s.commit()
        return int(d.id)


def test_go_marks_stale_when_summary_is_old():
    did = _seed_device("CF:00:00:00:00:01")
    _seed_summary(did, date.today() - timedelta(days=10), status_score=55)
    r = client.get("/devices/CF:00:00:00:00:01/go")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["stale"] is True
    assert data["asOf"] == (date.today() - timedelta(days=10)).isoformat()
    assert data["healthStatus"]["status"] == 55  # value still returned


def test_go_fresh_when_summary_is_recent():
    did = _seed_device("CF:00:00:00:00:02")
    _seed_summary(did, date.today(), status_score=72)
    r = client.get("/devices/CF:00:00:00:00:02/go")
    data = r.json()["data"]
    assert data["stale"] is False
    assert data["asOf"] == date.today().isoformat()


def test_go_no_data_is_marked():
    _seed_device("CF:00:00:00:00:03")  # device exists, no summary at all
    r = client.get("/devices/CF:00:00:00:00:03/go")
    data = r.json()["data"]
    assert data["asOf"] is None
    assert data["stale"] is True
    assert data["healthStatus"]["status"] == 0


def test_readiness_carries_gap(monkeypatch):
    # Force the "daily" source so we can drive it from DailySummary rows.
    monkeypatch.setattr("newboom.boom.status.READINESS_GRANULARITY", "daily")
    did = _seed_device("CF:00:00:00:00:04")
    _seed_summary(did, date(2026, 10, 1), status_score=40)
    _seed_summary(did, date(2026, 10, 5), status_score=80)
    r = client.get(
        "/devices/CF:00:00:00:00:04/readiness",
        params={"startDate": "2026-10-01", "endDate": "2026-10-05"},
    )
    assert r.status_code == 200
    by_date = {p["date"]: p for p in r.json()["data"]}
    assert by_date["2026-10-01"]["carried"] is False
    assert by_date["2026-10-01"]["value"] == 40
    assert by_date["2026-10-05"]["carried"] is False
    assert by_date["2026-10-05"]["value"] == 80
    # 10-02..10-04 are a 3-day gap (<= 7) carried from the 10-01 value.
    for d in ("2026-10-02", "2026-10-03", "2026-10-04"):
        assert by_date[d]["carried"] is True
        assert by_date[d]["value"] == 40


def test_sleep_trend_carries_gap():
    did = _seed_device("CF:00:00:00:00:05")
    # Two nights with a 3-day gap between them; the gap should be carried.
    with Session(engine) as s:
        for d in (date(2026, 10, 1), date(2026, 10, 5)):
            s.add(
                Sleep(
                    device_id=did,
                    timezone="08:00",
                    time=datetime(d.year, d.month, d.day, 23, 0),
                    sleep_onset_time=0,
                    awake_time=0,
                    light_sleep_period=3600,
                    deep_sleep_period=1800,
                    other_sleep_period=600,
                    heart_rate_rest=60,
                )
            )
        s.commit()
    r = client.get(
        "/devices/CF:00:00:00:00:05/sleep_trend",
        params={"startDate": "2026-10-01", "endDate": "2026-10-05"},
    )
    assert r.status_code == 200
    by_date = {p["date"]: p for p in r.json()["data"]}
    assert by_date["2026-10-01"]["carried"] is False
    assert by_date["2026-10-05"]["carried"] is False
    assert by_date["2026-10-01"]["value"] == round((3600 + 1800 + 600) / 3600.0, 1)
    for d in ("2026-10-02", "2026-10-03", "2026-10-04"):
        assert by_date[d]["carried"] is True
        assert by_date[d]["value"] == by_date["2026-10-01"]["value"]

"""Regression tests for the startup integrity gate in ``newboom.database``.

The gate must (a) detect a corrupt SQLite file, (b) pick the newest *valid*
backup, (c) replace the corrupt db atomically while leaving the backup intact,
and (d) refuse to start when no valid backup exists — so a corrupt db never
serves bad data. See docs/db-recovery-runbook.md.
"""

import json
import os
import sqlite3
import time
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select

import newboom.database as db
from newboom.database import (
    _integrity_ok,
    _newest_good_backup,
    _restore,
    ensure_db_integrity,
)
from newboom.model import PPI, Device, Sleep
from newboom.upload import PPIUploadRequest, SleepUploadRequest


def _make_good(path) -> None:
    """A small multi-page SQLite db with enough rows to span several pages."""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t(x INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5000)])
    con.commit()
    con.close()


def _corrupt(path) -> None:
    """Clobber a used b-tree page in the middle so integrity_check fails."""
    with open(str(path), "r+b") as f:
        f.seek(os.path.getsize(path) // 2)
        f.write(b"\xff" * 256)


def _backup(src, dst) -> None:
    """A consistent online backup of ``src`` to ``dst`` via SQLite's .backup API."""
    s = sqlite3.connect(str(src))
    b = sqlite3.connect(str(dst))
    s.backup(b)
    b.commit()
    s.close()
    b.close()


def _put(updir, name, req, mtime) -> None:
    """Write a ppi/sleep upload JSON and stamp its mtime (deterministic cutoff)."""
    p = updir / name
    p.write_text(json.dumps(req.model_dump()), encoding="utf-8")
    os.utime(p, (mtime, mtime))


@pytest.fixture(autouse=True)
def _gate_env(tmp_path, monkeypatch):
    """Gate on + quick check for every test; isolate UPLOAD_DIR to an empty dir.

    The recovery path re-ingests ppi/sleep from UPLOAD_DIR after a restore, so an
    empty per-test uploads dir keeps the gate tests hermetic (no real
    ``data/uploads`` scan). The re-ingest tests point UPLOAD_DIR at their own dir.
    """
    monkeypatch.setenv("DB_INTEGRITY_CHECK", "1")
    monkeypatch.setenv("INTEGRITY_CHECK_MODE", "quick")
    updir = tmp_path / "uploads"
    updir.mkdir()
    monkeypatch.setenv("UPLOAD_DIR", str(updir))


def test_integrity_ok_true_for_good(tmp_path):
    good = tmp_path / "good.db"
    _make_good(good)
    assert _integrity_ok(str(good)) is True


def test_integrity_ok_false_for_corrupt(tmp_path):
    bad = tmp_path / "bad.db"
    _make_good(bad)
    _corrupt(bad)
    assert _integrity_ok(str(bad)) is False


def test_newest_good_backup_picks_newest_valid(tmp_path):
    bdir = tmp_path / "backups"
    bdir.mkdir()
    old = bdir / "old.bak"  # older, corrupt
    _make_good(old)
    _corrupt(old)
    os.utime(old, (1_000_000, 1_000_000))
    new = bdir / "new.bak"  # newer, valid
    _make_good(new)
    os.utime(new, (2_000_000, 2_000_000))
    live = tmp_path / "live.db"
    _make_good(live)
    _corrupt(live)
    result = _newest_good_backup(str(bdir), str(live))
    assert result is not None
    assert os.path.basename(result) == "new.bak"


def test_newest_good_backup_none_when_all_corrupt(tmp_path):
    bdir = tmp_path / "backups"
    bdir.mkdir()
    for i in range(3):
        b = bdir / f"b{i}.bak"
        _make_good(b)
        _corrupt(b)
        os.utime(b, (1_000_000 + i, 1_000_000 + i))
    live = tmp_path / "live.db"
    _make_good(live)
    _corrupt(live)
    assert _newest_good_backup(str(bdir), str(live)) is None


def test_newest_good_backup_ignores_live_file(tmp_path):
    # A *.bak that IS the live file must never be restored onto itself.
    bdir = tmp_path / "backups"
    bdir.mkdir()
    live = bdir / "live.db"
    _make_good(live)
    assert _newest_good_backup(str(bdir), str(live)) is None


def test_restore_replaces_corrupt_and_keeps_backup(tmp_path):
    good = tmp_path / "good.db"
    _make_good(good)
    live = tmp_path / "live.db"
    _make_good(live)
    _corrupt(live)
    assert _integrity_ok(str(live)) is False
    _restore(str(good), str(live))
    assert _integrity_ok(str(live)) is True  # live now sound
    assert _integrity_ok(str(good)) is True  # backup untouched
    # no temp file left behind
    assert not (tmp_path / "live.db.restore-tmp").exists()


def test_ensure_db_integrity_restores_on_corrupt(tmp_path, monkeypatch):
    bdir = tmp_path / "backups"
    bdir.mkdir()
    good = bdir / "good.bak"
    _make_good(good)
    live = tmp_path / "live.db"
    _make_good(live)
    _corrupt(live)
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{live}"))
    monkeypatch.setenv("BACKUP_DIR", str(bdir))
    ensure_db_integrity()
    assert _integrity_ok(str(live)) is True


def test_ensure_db_integrity_refuses_without_backup(tmp_path, monkeypatch):
    live = tmp_path / "live.db"
    _make_good(live)
    _corrupt(live)
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{live}"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError):
        ensure_db_integrity()
    assert _integrity_ok(str(live)) is False  # untouched: no restore attempted


def test_ensure_db_integrity_healthy_is_noop(tmp_path, monkeypatch):
    # A healthy db must not trigger a restore, even with no backup present.
    good = tmp_path / "good.db"
    _make_good(good)
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{good}"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "empty"))
    ensure_db_integrity()  # must not raise
    assert _integrity_ok(str(good)) is True


def test_ensure_db_integrity_disabled_is_noop(tmp_path, monkeypatch):
    live = tmp_path / "live.db"
    _make_good(live)
    _corrupt(live)
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{live}"))
    monkeypatch.setenv("DB_INTEGRITY_CHECK", "0")
    ensure_db_integrity()  # disabled: must not touch the corrupt db
    assert _integrity_ok(str(live)) is False


def test_reingest_applies_post_backup_uploads(tmp_path):
    """After a restore, ppi/sleep uploads newer than the backup are re-applied
    (idempotent upsert on ``(device_id, time)``)."""
    from newboom.boom.upload import reingest_uploads_after

    live = tmp_path / "live.db"
    updir = tmp_path / "uploads"
    eng = create_engine(f"sqlite:///{live}")
    SQLModel.metadata.create_all(eng)
    bm = time.time() + 100.0  # "backup mtime"; every upload below is newer
    _put(
        updir,
        "D.ppi.json",
        PPIUploadRequest(
            device="D",
            address="AA:BB:CC:DD:EE:01",
            timezone="08:00",
            datas=[{"time": "2026-01-02 03:00:00", "hr": 70, "spo2": 960, "ppi": 900, "activity": 1}],
        ),
        bm + 1,
    )
    _put(
        updir,
        "D.sleep.json",
        SleepUploadRequest(
            device="D",
            address="AA:BB:CC:DD:EE:01",
            timezone="08:00",
            datas=[
                {
                    "time": "2026-01-02 00:00:00",
                    "sleepOnsetTime": 6000,
                    "awakeTime": 300,
                    "lightSleepPeriod": 3000,
                    "deepSleepPeriod": 2000,
                    "otherSleepPeriod": 500,
                    "heartRateRest": 60,
                }
            ],
        ),
        bm + 1,
    )
    ppi, sleep, skipped = reingest_uploads_after(eng, str(updir), bm)
    assert (ppi, sleep, skipped) == (1, 1, 0)
    with Session(eng) as s:
        assert s.exec(select(PPI)).first() is not None
        assert s.exec(select(Sleep)).first() is not None


def test_reingest_skips_pre_backup_and_malformed(tmp_path):
    """Uploads older than the backup are left alone; a malformed file is skipped
    (counted) without aborting the good ones."""
    from newboom.boom.upload import reingest_uploads_after

    live = tmp_path / "live.db"
    updir = tmp_path / "uploads"
    eng = create_engine(f"sqlite:///{live}")
    SQLModel.metadata.create_all(eng)
    bm = time.time() + 100.0
    # pre-backup (mtime < bm): its data must NOT appear
    _put(
        updir,
        "old.ppi.json",
        PPIUploadRequest(
            device="D",
            address="AA:BB:CC:DD:EE:02",
            timezone="08:00",
            datas=[{"time": "2026-01-03 03:00:00", "hr": 11, "spo2": 11, "ppi": 11, "activity": 0}],
        ),
        bm - 1,
    )
    # malformed but newer than the backup: skipped + counted, must not abort
    bad = updir / "bad.ppi.json"
    bad.write_text("{not valid json", encoding="utf-8")
    os.utime(bad, (bm + 1, bm + 1))
    # a good post-backup upload that must still be applied despite the bad one
    _put(
        updir,
        "good.ppi.json",
        PPIUploadRequest(
            device="D",
            address="AA:BB:CC:DD:EE:02",
            timezone="08:00",
            datas=[{"time": "2026-01-04 03:00:00", "hr": 80, "spo2": 950, "ppi": 880, "activity": 0}],
        ),
        bm + 2,
    )
    ppi, sleep, skipped = reingest_uploads_after(eng, str(updir), bm)
    assert ppi == 1  # only good.ppi.json
    assert sleep == 0
    assert skipped == 1  # the malformed file
    with Session(eng) as s:
        assert s.exec(select(PPI).where(PPI.hr == 80)).first() is not None
        assert s.exec(select(PPI).where(PPI.hr == 11)).first() is None  # pre-backup skipped


def test_gate_reingests_after_restore(tmp_path, monkeypatch):
    """End-to-end: on a corrupt db the gate restores from the backup AND
    re-ingests post-backup ppi, closing the gap the backup alone can't cover."""
    bdir = tmp_path / "backups"
    bdir.mkdir()
    updir = tmp_path / "uploads"
    live = tmp_path / "live.db"
    eng = create_engine(f"sqlite:///{live}")
    SQLModel.metadata.create_all(eng)
    # seed one pre-backup ppi row
    with Session(eng) as s:
        dev = Device(name="D", mac="AA:BB:CC:DD:EE:03", tz="08:00")
        s.add(dev)
        s.commit()
        s.refresh(dev)
        s.add(
            PPI(
                time=datetime(2026, 1, 1, 3, 0, 0),
                hr=50,
                spo2=900,
                ppi=800,
                activity=0,
                device_id=dev.id,
            )
        )
        s.commit()
    good = bdir / "good.bak"
    _backup(live, good)
    bm = os.stat(good).st_mtime + 1.0
    _put(
        updir,
        "D.ppi.json",
        PPIUploadRequest(
            device="D",
            address="AA:BB:CC:DD:EE:03",
            timezone="08:00",
            datas=[{"time": "2026-01-02 03:00:00", "hr": 70, "spo2": 960, "ppi": 900, "activity": 1}],
        ),
        bm + 1,
    )
    _corrupt(live)
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{live}"))
    monkeypatch.setenv("BACKUP_DIR", str(bdir))
    monkeypatch.setenv("UPLOAD_DIR", str(updir))
    ensure_db_integrity()
    with Session(db.engine) as s:
        rows = s.exec(select(PPI)).all()
    assert len(rows) == 2  # pre-backup restored + post-backup re-ingested
    assert any(r.hr == 70 for r in rows)

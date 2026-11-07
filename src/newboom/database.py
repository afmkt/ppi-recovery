from __future__ import annotations

import os
import random
import time

from dotenv import load_dotenv
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine

load_dotenv()
sqlite_url = os.getenv("DATABASE_URL", "sqlite:///data/newboom.db")


# One shared engine. It is used by both the async event-loop thread (FastAPI
# request sessions via get_session) and the off-loop worker thread
# (asyncio.to_thread), so the pool's connections must be allowed to cross thread
# boundaries (check_same_thread=False). The per-connection busy-wait lives in the
# connect listener below (PRAGMA busy_timeout) — the only thing connect_args still
# needs is check_same_thread, which the listener cannot set.
engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, _record):
    """Put every new connection into WAL mode at a crash-safe sync level.

    WAL (write-ahead logging) is the fix for the worker/API contention: a writer
    runs alongside readers and, crucially, a reader never blocks a writer. WAL is
    a persistent property of the .db file, so one connection setting it is enough,
    but the connect listener guarantees it on every new connection regardless of
    which one touches the database first. synchronous=NORMAL is the crash-safe
    level *with* WAL; do not use it in rollback-journal mode, where it risks
    corruption on power loss. Requires the file to live on a local,
    mmap-capable filesystem (not NFS/SMB).
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")  # good tradeoff with WAL
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


# --- Concurrency retry -------------------------------------------------------
#
# WAL plus the connect_args busy-timeout above are the first line of defence
# against the worker and the API contending for SQLite's single writer lock. A
# transient lock that outlives the busy-wait surfaces as OperationalError
# ("database is locked"/"database is busy"); the helpers below retry such a unit.
# A retry is safe only because a SQLite commit is atomic: a failed attempt wrote
# nothing, so re-running an idempotent unit (upserts, watermark-guarded stages,
# idempotent file writes) neither duplicates nor loses data.


def is_transient_lock(exc: BaseException) -> bool:
    """True for a transient SQLite contention error worth retrying.

    Only the busy-handler timing out under concurrent writers is retried. Other
    OperationalErrors — "no such table", "database is corrupt", constraint
    violations — are permanent and must propagate, not be retried.
    """
    if not isinstance(exc, OperationalError):
        return False
    msg = " ".join(str(a).lower() for a in getattr(exc, "args", ()))
    return "locked" in msg or "busy" in msg


def retry(work, *, attempts: int = 5, base_delay: float = 0.05, max_delay: float = 1.0):
    """Run the re-runnable ``work()`` callback, retrying on a transient SQLite
    lock/timeout with exponential backoff + jitter.

    ``work`` must be idempotent (see the module note): re-running it after a
    failed commit is safe. Returns ``work``'s value; re-raises the final error
    or any non-transient error. Use this for callbacks that open their own
    Session(s); for a caller-supplied session use :func:`run_write`.
    """
    delay = base_delay
    for attempt in range(attempts):
        try:
            return work()
        except OperationalError as exc:
            if is_transient_lock(exc) and attempt < attempts - 1:
                time.sleep(min(random.uniform(0, delay), max_delay))
                delay = min(delay * 2, max_delay)
                continue
            raise


def run_write(
    session: Session,
    work,
    *,
    attempts: int = 5,
    base_delay: float = 0.05,
    max_delay: float = 1.0,
):
    """Run ``work(session)`` — which performs its own add/execute/commit — and
    retry the whole unit on a transient SQLite lock/timeout.

    ``work`` must be re-runnable (idempotent inserts, idempotent side effects);
    a failed SQLite commit wrote nothing, so a retry neither duplicates nor loses
    data. The supplied ``session`` is rolled back between attempts. Returns
    ``work``'s value.
    """

    def _unit():
        try:
            return work(session)
        except OperationalError:
            session.rollback()
            raise

    return retry(_unit, attempts=attempts, base_delay=base_delay, max_delay=max_delay)


def create_db_and_tables():
    import newboom.model  # noqa: F401  register table metadata before create_all

    SQLModel.metadata.create_all(engine)
    _migrate_add_columns()
    _migrate_drop_columns()


# Columns added after the initial schema. create_all() only creates missing
# tables, so new columns on existing tables must be added explicitly.
_NEW_COLUMNS: dict[str, dict[str, str]] = {
    "device": {
        "tz": "VARCHAR",
        "is_female": "BOOLEAN DEFAULT 0",
        "age": "INTEGER",
    },
    "minutemetric": {
        "load_delta": "FLOAT DEFAULT 0",
        "tss_delta": "FLOAT DEFAULT 0",
    },
    "dailysummary": {
        "total_load": "FLOAT",
        "total_tss": "FLOAT",
        "spo2_night_avg": "FLOAT",
        "spo2_night_min": "FLOAT",
        "status_morning": "INTEGER",
        "status_current": "INTEGER",
        "deduction_points": "INTEGER DEFAULT 0",
        "bonus_points": "INTEGER DEFAULT 0",
        "energy_incomplete": "BOOLEAN DEFAULT 0",
        "last_valid_energy_ts": "DATETIME",
        "sympathetic_pct": "FLOAT",
        "parasympathetic_pct": "FLOAT",
        "transition_pct": "FLOAT",
        # P0-1: per-day raw metric values, kept separate from the 7-day
        # rolling baselines stored in the *_avg/*_std fields.
        "hrv_night_val": "FLOAT",
        "hrv_morning_val": "FLOAT",
        "rest_heart_rate_val": "FLOAT",
        "non_sport_sleep_hr_val": "FLOAT",
        "bed_time_val": "FLOAT",
    },
    "windowmetric": {
        "is_sleep": "BOOLEAN",
    },
}


def _migrate_add_columns() -> None:
    """Add any columns from the current model that are missing on existing tables."""
    with Session(engine) as session:
        for table, columns in _NEW_COLUMNS.items():
            existing = {
                row[1] for row in session.execute(text(f"PRAGMA table_info({table})"))
            }
            for column, ddl in columns.items():
                if column not in existing:
                    session.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    )
                    session.commit()


# Columns removed from the model after the schema was first written. create_all()
# never drops a column, and SQLite rejects ``ALTER TABLE DROP COLUMN`` for a
# column that is part of a foreign-key definition, so an obsolete column is shed
# by rebuilding its table from the surviving columns. Each entry is a no-op once
# the column is already gone.
_DROPPED_COLUMNS: dict[str, list[str]] = {
    "questionnaireanswer": ["device_id"],
}


def _column_def(row) -> str:
    """Rebuild a ``CREATE TABLE`` column clause from a ``table_info`` row.

    ``row`` is ``(cid, name, type, notnull, dflt_value, pk)``.
    """
    _cid, name, ctype, notnull, dflt, pk = row
    parts = [name, ctype or "TEXT"]
    if pk:
        parts.append("PRIMARY KEY")
    elif notnull:
        parts.append("NOT NULL")
    if dflt is not None:
        parts.append(f"DEFAULT {dflt}")
    return " ".join(parts)


def _migrate_drop_columns() -> None:
    """Shed columns removed from the model from pre-existing tables."""
    with engine.begin() as conn:
        for table, cols in _DROPPED_COLUMNS.items():
            info = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            present = {row[1] for row in info}
            drop = [c for c in cols if c in present]
            if not drop:
                continue
            keep = [row for row in info if row[1] not in drop]
            defs = [_column_def(row) for row in keep]
            names = ", ".join(row[1] for row in keep)
            conn.exec_driver_sql(f"CREATE TABLE {table}_new ({', '.join(defs)})")
            conn.exec_driver_sql(
                f"INSERT INTO {table}_new ({names}) SELECT {names} FROM {table}"
            )
            conn.exec_driver_sql(f"DROP TABLE {table}")
            conn.exec_driver_sql(f"ALTER TABLE {table}_new RENAME TO {table}")


def get_session():
    from newboom.database import engine

    with Session(engine) as session:
        yield session

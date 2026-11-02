from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

load_dotenv()
sqlite_url = os.getenv("DATABASE_URL", "sqlite:///data/newboom.db")


engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})


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

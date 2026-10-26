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


def get_session():
    from newboom.database import engine

    with Session(engine) as session:
        yield session

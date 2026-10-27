"""Pytest harness for the newboom algorithm/data layer.

The engine (and every module that imports it) is created at import time from
``DATABASE_URL``, so the env var must be set *before* importing
``newboom.database``. A per-test truncation gives each test an empty schema.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="newboom_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from newboom.database import create_db_and_tables, engine  # noqa: E402
from newboom.model import (  # noqa: E402
    AutonomicEpisode,
    DailySummary,
    Device,
    HourlyStatus,
    MinuteMetric,
    PPI,
    Progress,
    RmssdSegment,
    Sleep,
    WindowMetric,
)

create_db_and_tables()

# Insert order is irrelevant: the test engine does not enable FK enforcement,
# so plain DELETE per table is enough for isolation.
_TABLES = [
    Device,
    PPI,
    Sleep,
    WindowMetric,
    RmssdSegment,
    AutonomicEpisode,
    MinuteMetric,
    DailySummary,
    HourlyStatus,
    Progress,
]


@pytest.fixture(autouse=True)
def clean_db():
    """Empty every table before each test."""
    with engine.begin() as conn:
        for table in _TABLES:
            conn.execute(text(f"DELETE FROM {table.__tablename__}"))
    yield

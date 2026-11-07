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
    PPI,
    Conversation,
    DailySummary,
    DataGap,
    Device,
    HourlyStatus,
    LayerFreshness,
    Message,
    MinuteMetric,
    Progress,
    QuestionnaireAnswer,
    Reconciliation,
    RmssdSegment,
    Sleep,
    WindowMetric,
)

create_db_and_tables()

# FK enforcement is ON (database._configure_sqlite runs PRAGMA foreign_keys=ON),
# so a parent cannot be deleted while child rows remain. _TABLES lists parents
# first; clean_db deletes in reverse so every child row is gone before its parent.
_TABLES = [
    Device,
    PPI,
    Sleep,
    WindowMetric,
    RmssdSegment,
    MinuteMetric,
    DailySummary,
    HourlyStatus,
    QuestionnaireAnswer,
    Progress,
    Conversation,
    Message,
    LayerFreshness,
    DataGap,
    Reconciliation,
]


@pytest.fixture(autouse=True)
def clean_db():
    """Empty every table before each test.

    Children are deleted before parents (``reversed``) because the engine enforces
    foreign keys; deleting a parent while its child rows remain raises an
    IntegrityError.
    """
    with engine.begin() as conn:
        for table in reversed(_TABLES):
            conn.execute(text(f"DELETE FROM {table.__tablename__}"))
    yield

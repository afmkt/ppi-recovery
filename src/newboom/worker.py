from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import Session, select

from newboom.compute import process_pipeline
from newboom.database import create_db_and_tables, engine
from newboom.model import Device

logger = logging.getLogger("newboom.worker")

# Worker cadence and the staleness window after which a worker that has not
# completed a cycle is reported unhealthy by /healthz. Both are env-tunable, so
# a slow environment can widen the grace without a code change.
WORKER_INTERVAL_S = float(os.getenv("WORKER_INTERVAL_S", "10"))
WORKER_STALE_S = float(os.getenv("WORKER_STALE_S", "30"))


class WorkerHealth:
    """Per-cycle outcome tracker for the background worker loop.

    The loop records each cycle's success/failure here so a health check can
    observe a *persistently* failing worker (e.g. a read-only database that
    makes every ``UPDATE`` raise) instead of the container staying "healthy"
    while the pipeline silently no-ops.

    Healthy == a cycle succeeded within ``stale_after_s`` seconds. A single
    transient failure is tolerated (the next success clears it); a sustained
    failure flips the check unhealthy after one stale window. A worker task
    that dies also surfaces as unhealthy via the same staleness, since no
    further cycle updates the last-success time.
    """

    def __init__(self, stale_after_s: float = WORKER_STALE_S) -> None:
        self.stale_after_s = stale_after_s
        self._lock = threading.Lock()
        self.started_at: float | None = None
        self.last_success_ts: float | None = None
        self.last_cycle_ts: float | None = None
        self.last_error: str | None = None
        self.consecutive_failures: int = 0
        self.total_cycles: int = 0
        self.total_failures: int = 0

    def reset(self) -> None:
        with self._lock:
            self.started_at = None
            self.last_success_ts = None
            self.last_cycle_ts = None
            self.last_error = None
            self.consecutive_failures = 0
            self.total_cycles = 0
            self.total_failures = 0

    def mark_started(self) -> None:
        with self._lock:
            self.started_at = time.monotonic()

    def record_success(self) -> None:
        with self._lock:
            now = time.monotonic()
            self.last_success_ts = now
            self.last_cycle_ts = now
            self.consecutive_failures = 0
            self.total_cycles += 1

    def record_failure(self, err: BaseException) -> None:
        with self._lock:
            self.last_cycle_ts = time.monotonic()
            self.last_error = f"{type(err).__name__}: {err}"
            self.consecutive_failures += 1
            self.total_cycles += 1
            self.total_failures += 1

    def is_healthy(self) -> bool:
        with self._lock:
            if self.last_success_ts is None:
                return False
            return (time.monotonic() - self.last_success_ts) <= self.stale_after_s

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            since = (
                None
                if self.last_success_ts is None
                else time.monotonic() - self.last_success_ts
            )
            return {
                "started": self.started_at is not None,
                "last_error": self.last_error,
                "consecutive_failures": self.consecutive_failures,
                "total_cycles": self.total_cycles,
                "total_failures": self.total_failures,
                "seconds_since_last_success": since,
                "stale_after_s": self.stale_after_s,
            }


# One process-wide tracker: read by the /healthz endpoint and written by the
# worker task (a different thread), hence the internal lock.
worker_health = WorkerHealth()


async def _run_cycle_once() -> None:
    """Run one pipeline pass over all devices, recording its outcome.

    Never raises: a failed cycle is recorded on ``worker_health`` (and logged
    with its stack trace) but does not stop the loop, so one bad cycle cannot
    kill the worker.
    """
    try:
        with Session(engine) as session:
            all_devices = session.exec(select(Device)).all()
            process_pipeline(list(all_devices))
        worker_health.record_success()
    except Exception as e:  # a bad cycle must not kill the loop
        worker_health.record_failure(e)
        logger.exception("background worker cycle failed")


async def background_worker_loop() -> None:
    """Continuously reprocess unprocessed devices every ``WORKER_INTERVAL_S``.

    Each cycle's outcome is recorded on ``worker_health`` so a persistently
    failing worker surfaces via ``/healthz`` (and the container health check)
    instead of looping forever while silently failing to persist.
    """
    worker_health.mark_started()
    while True:
        await _run_cycle_once()
        await asyncio.sleep(WORKER_INTERVAL_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the schema exists before any query runs. This must live in the
    # lifespan, NOT an @app.on_event("startup") handler: passing a custom
    # lifespan= to FastAPI replaces Starlette's default startup, so on_event
    # handlers never fire and the tables were previously never created.
    create_db_and_tables()
    # Start the background worker task when the app starts
    worker_task = asyncio.create_task(background_worker_loop())
    yield
    # Cancel the task gracefully when the app shuts down
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

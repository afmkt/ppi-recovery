from __future__ import annotations

import asyncio
import logging
import os
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

# Run the Q1–Q6 reporting read-models (record_diagnostics) on at most every
# Nth cycle. They're dashboard-only health reports, not pipeline inputs, so
# skipping them on idle cycles keeps a frozen device near-zero-cost. Throttled
# by cycle count, not wall-clock: with WORKER_INTERVAL_S=10, N=6 ≈ 1 min.
# Clamped to >=1 so a bad env value can't cause a ZeroDivisionError.
DIAGNOSTICS_EVERY_N_CYCLES = max(1, int(os.getenv("DIAGNOSTICS_EVERY_N_CYCLES", "6")))


async def background_worker_loop() -> None:

    cycle = 0
    while True:
        try:
            # The Q1–Q6 reporting read-models are throttled to every
            # DIAGNOSTICS_EVERY_N_CYCLES-th cycle; processing runs every cycle.
            run_diagnostics = (cycle % DIAGNOSTICS_EVERY_N_CYCLES) == 0
            # The pipeline is CPU- and I/O-bound and, if run inline, would block
            # the single uvicorn event loop for the whole cycle (~seconds),
            # freezing /api/upload, the boom endpoints, the dashboard and even
            # /healthz. Offloading it to a worker thread keeps the loop free to
            # serve HTTP between cycles. Each pipeline stage opens its own
            # Session, so the thread is independent of the loop's sessions.
            await asyncio.to_thread(_run_pipeline_cycle, run_diagnostics)

        except Exception:  # a bad cycle must not kill the loop
            logger.exception("background worker cycle failed")
        cycle += 1
        await asyncio.sleep(WORKER_INTERVAL_S)


def _run_pipeline_cycle(run_diagnostics: bool) -> None:
    """One worker cycle, executed off the event loop by ``to_thread``."""
    with Session(engine) as session:
        all_devices = session.exec(select(Device)).all()
        process_pipeline(list(all_devices), run_diagnostics=run_diagnostics)


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

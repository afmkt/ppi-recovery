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


async def background_worker_loop() -> None:

    while True:
        try:
            with Session(engine) as session:
                all_devices = session.exec(select(Device)).all()
                process_pipeline(list(all_devices))

        except Exception as e:  # a bad cycle must not kill the loop
            logger.exception("background worker cycle failed")
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

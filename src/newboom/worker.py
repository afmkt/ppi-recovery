from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import Session, select

from newboom.compute import process_pipeline
from newboom.database import engine
from newboom.model import Device


async def background_worker_loop():
    """
    Continuous loop that checks for unprocessed devices every 10 seconds.
    """
    while True:
        try:
            with Session(engine) as session:
                all_devices = session.exec(select(Device)).all()
                process_pipeline(all_devices)
        except Exception as e:
            print(f"Error in background worker loop: {e}")

        # Sleep for 10 seconds before checking again
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the background worker task when the app starts
    worker_task = asyncio.create_task(background_worker_loop())
    yield
    # Cancel the task gracefully when the app shuts down
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

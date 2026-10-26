from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, col, select

from newboom.database import get_session
from newboom.model import PPI, ActivityState, Device, Sleep
from newboom.schema import ApiResponse
from newboom.storage import list_uploaded_files, load_data_from_file, save_data_to_file


def midnight_days_ago(days: int) -> datetime:
    """Naive local midnight a specified number of days ago (matches local storage)."""
    now = datetime.now()
    days_ago = now - timedelta(days=days)
    return days_ago.replace(hour=0, minute=0, second=0, microsecond=0)


router = APIRouter()

"""
{
	"device": "BOOM-0001",
	"address": "AA:BB:CC:DD:EE:FF",
	"timezone": "08:00",
	"datas": [
        { 
            "time": "2026-10-08 02:13:07", 
            "hr": 62, 
            "spo2": 950, 
            "ppi": 980, 
            "activity": 1 
        }
    ]
}

see docs/new-interface.md for the new interface design
"""


class PPIRow(BaseModel):
    time: str
    hr: int
    spo2: int
    ppi: int
    activity: ActivityState


class PPIUploadRequest(BaseModel):
    device: str
    address: str
    timezone: str
    datas: list[PPIRow]


@router.get("/ppi", response_model=ApiResponse)
async def get_ppi(
    start: datetime | None = None,
    end: datetime | None = None,
    skip: int = Query(
        default=0, ge=0, description="Number of records to skip (for pagination)"
    ),
    limit: int = Query(
        default=1000, ge=1, le=10000, description="Max number of records to return"
    ),
    session: Session = Depends(get_session),
):
    """
    Endpoint to retrieve PPI data within a specified time range.
    """

    query = select(PPI)
    if not start:
        start = midnight_days_ago(7)
    if start:
        query = query.where(PPI.time >= start)
    if end:
        query = query.where(PPI.time <= end)
    query = query.order_by(col(PPI.time))
    query = query.offset(skip).limit(limit)
    ppi_data = session.exec(query).all()

    return ApiResponse(
        ok=True,
        data=[ppi.model_dump() for ppi in ppi_data],
    )


@router.post("/ppi", response_model=ApiResponse)
async def upload_ppi(
    ppi_data: PPIUploadRequest, session: Session = Depends(get_session)
):
    # Check if the device exists, if not, create it
    device = session.exec(select(Device).where(Device.mac == ppi_data.address)).first()
    if not device:
        device = Device(name=ppi_data.device, mac=ppi_data.address, tz=ppi_data.timezone)
        session.add(device)
        session.commit()
        session.refresh(device)
    elif device.tz != ppi_data.timezone:
        device.tz = ppi_data.timezone
        session.add(device)
        session.commit()
        session.refresh(device)

    # Save data to file
    filename = save_data_to_file(ppi_data.model_dump(), "ppi", ppi_data.device)

    # Insert PPI data
    for row in ppi_data.datas:
        local_time = datetime.strptime(row.time, "%Y-%m-%d %H:%M:%S")
        stmt = (
            insert(PPI)
            .values(
                time=local_time,
                hr=row.hr,
                spo2=row.spo2,
                ppi=row.ppi,
                activity=row.activity,
                device_id=device.id or 0,
            )
            .on_conflict_do_update(
                index_elements=["device_id", "time"],
                set_={
                    "hr": row.hr,
                    "spo2": row.spo2,
                    "ppi": row.ppi,
                    "activity": row.activity,
                    "fresh": True,
                },
            )
        )
        session.execute(stmt)
    session.commit()
    return ApiResponse(
        ok=True,
        message="PPI data uploaded successfully",
        file=filename,
        device_id=device.id,
        processed=len(ppi_data.datas),
    )


"""
{
	"device": "BOOM-0001",
	"address": "AA:BB:CC:DD:EE:FF",
	"timezone": "08:00",
	"datas": [
		{
			"time": "2026-10-08 07:30:00",
			"sleepOnsetTime": 25200,
			"awakeTime": 1800,
			"lightSleepPeriod": 14400,
			"deepSleepPeriod": 7200,
			"otherSleepPeriod": 1800,
			"heartRateRest": 55
		}
	]
}

see docs/new-interface.md for the new interface design
"""


class SleepRow(BaseModel):
    time: str
    sleepOnsetTime: int
    awakeTime: int
    lightSleepPeriod: int
    deepSleepPeriod: int
    otherSleepPeriod: int
    heartRateRest: int


class SleepUploadRequest(BaseModel):
    device: str
    address: str
    timezone: str
    datas: list[SleepRow]


@router.get("/sleep", response_model=ApiResponse)
async def get_sleep(
    start: datetime | None = None,
    end: datetime | None = None,
    skip: int = Query(
        default=0, ge=0, description="Number of records to skip (for pagination)"
    ),
    limit: int = Query(
        default=1000, ge=1, le=10000, description="Max number of records to return"
    ),
    session: Session = Depends(get_session),
):
    """
    Endpoint to retrieve Sleep data within a specified time range.
    """

    query = select(Sleep)
    if not start:
        start = midnight_days_ago(7)
    if start:
        query = query.where(Sleep.time >= start)
    if end:
        query = query.where(Sleep.time <= end)
    query = query.order_by(col(Sleep.time))
    query = query.offset(skip).limit(limit)
    sleep_data = session.exec(query).all()

    return ApiResponse(
        ok=True,
        data=[sleep.model_dump() for sleep in sleep_data],
    )


@router.post("/sleep", response_model=ApiResponse)
async def upload_sleep(
    sleep_data: SleepUploadRequest, session: Session = Depends(get_session)
):
    # Check if the device exists, if not, create it
    device = session.exec(
        select(Device).where(Device.mac == sleep_data.address)
    ).first()
    if not device:
        device = Device(name=sleep_data.device, mac=sleep_data.address, tz=sleep_data.timezone)
        session.add(device)
        session.commit()
        session.refresh(device)
    elif device.tz != sleep_data.timezone:
        device.tz = sleep_data.timezone
        session.add(device)
        session.commit()
        session.refresh(device)

    # Save data to file
    filename = save_data_to_file(sleep_data.model_dump(), "sleep", sleep_data.device)

    # Insert Sleep data
    for row in sleep_data.datas:
        local_time = datetime.strptime(row.time, "%Y-%m-%d %H:%M:%S")
        stmt = (
            insert(Sleep)
            .values(
                time=local_time,
                timezone=sleep_data.timezone,
                sleep_onset_time=row.sleepOnsetTime,
                awake_time=row.awakeTime,
                light_sleep_period=row.lightSleepPeriod,
                deep_sleep_period=row.deepSleepPeriod,
                other_sleep_period=row.otherSleepPeriod,
                heart_rate_rest=row.heartRateRest,
                device_id=device.id or 0,
            )
            .on_conflict_do_update(
                index_elements=["device_id", "time"],
                set_={
                    "timezone": sleep_data.timezone,
                    "sleep_onset_time": row.sleepOnsetTime,
                    "awake_time": row.awakeTime,
                    "light_sleep_period": row.lightSleepPeriod,
                    "deep_sleep_period": row.deepSleepPeriod,
                    "other_sleep_period": row.otherSleepPeriod,
                    "heart_rate_rest": row.heartRateRest,
                    "fresh": True,
                },
            )
        )
        session.execute(stmt)

    session.commit()
    return ApiResponse(
        ok=True,
        message="Sleep data uploaded successfully",
        device_id=device.id,
        processed=len(sleep_data.datas),
        file=filename,
    )


@router.get("/file/{file_path:path}", response_model=ApiResponse)
async def get_uploaded_file(file_path: str):
    """
    Endpoint to retrieve the uploaded file.
    """
    content = load_data_from_file(file_path)
    return ApiResponse(
        ok=True,
        file=file_path,
        data=content,
    )


@router.get("/file", response_model=ApiResponse)
async def list_uploaded_files_api(start: date | None = None, end: date | None = None):
    """
    Endpoint to list all uploaded files.
    """

    files = list_uploaded_files(start=start, end=end)
    return ApiResponse(
        ok=True,
        data=files,
    )

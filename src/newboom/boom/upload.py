"""10.x 蓝牙数据上传 — ``/upload/ppi`` & ``/upload/sleep``.

The §10 request shapes are superseded by ``docs/new-interface.md``:
``/upload/ppi`` carries the per-second sleep detail (``activity``) plus
``hr`` / ``spo2`` / ``ppi``; ``/upload/sleep`` carries the one-per-night
summary (onset/awake + three stage durations + resting HR). ``spo2`` is the
device raw value (×10); ``spo2 == 0`` means the device gave no sample that
second (the per-second history has no spo2 field, only the broadcast does).

The write logic mirrors :mod:`newboom.upload` (the legacy router mounted at
``/api/upload``); only the envelope differs here — writes return
``BoomMessage`` (``{code, msg}``), the retrieval GETs return
``BoomResponse[list[…]]``. ``spo2`` is stored/uploaded as the device raw
value (×10); only the UI folds it back to a percentage on display.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, col, select

from newboom.boom.schemas import BoomMessage, BoomResponse
from newboom.database import get_session
from newboom.model import PPI, Device, Sleep
from newboom.storage import save_data_to_file
from newboom.upload import (
    PPIRow,
    PPIUploadRequest,
    SleepRow,
    SleepUploadRequest,
    midnight_days_ago,
)

router = APIRouter(prefix="/upload", tags=["蓝牙上传"])

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _upsert_device(session: Session, name: str, address: str, timezone: str) -> Device:
    """Create or refresh the Device for ``address``; returns the row.

    Mirrors the legacy handler: a new device is inserted; an existing one is
    refreshed when its timezone changed.
    """
    device = session.exec(select(Device).where(Device.mac == address)).first()
    if not device:
        device = Device(name=name, mac=address, tz=timezone)
        session.add(device)
        session.commit()
        session.refresh(device)
    elif device.tz != timezone:
        device.tz = timezone
        session.add(device)
        session.commit()
        session.refresh(device)
    return device


# --- 10.1 上传心率数据（逐秒分期曲线 + 心率/血氧/PPI）---
@router.get("/ppi", response_model=BoomResponse[list[PPIRow]])
async def get_ppi(
    start: datetime | None = Query(None, description="起始时刻，不传默认近 7 天"),
    end: datetime | None = Query(None, description="结束时刻"),
    skip: int = Query(default=0, ge=0, description="跳过的记录数（分页）"),
    limit: int = Query(default=1000, ge=1, le=10000, description="返回的最大记录数"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[PPIRow]]:
    """10.1 获取 PPI 数据（按时间范围）.

    ``start`` defaults to local midnight 7 days ago when omitted; results are
    ordered by ``time`` then paginated by ``skip`` / ``limit``.
    """
    query = select(PPI)
    if not start:
        start = midnight_days_ago(7)
    if start:
        query = query.where(PPI.time >= start)
    if end:
        query = query.where(PPI.time <= end)
    query = query.order_by(col(PPI.time)).offset(skip).limit(limit)
    rows = session.exec(query).all()
    return BoomResponse(
        code=0,
        data=[
            PPIRow(
                time=r.time.strftime(_TIME_FMT),
                hr=r.hr,
                spo2=r.spo2,
                ppi=r.ppi,
                activity=r.activity,
            )
            for r in rows
        ],
    )


@router.post("/ppi", response_model=BoomMessage)
async def upload_ppi(
    req: PPIUploadRequest, session: Session = Depends(get_session)
) -> BoomMessage:
    """10.1 上传 PPI 数据（逐秒分期曲线 + 心率/血氧/PPI）.

    new-interface.md §3.1: ``spo2`` 为设备原始值(×10)，落库/上传均用原始值，
    仅界面展示时折算百分比；``spo2=0`` 表示该秒设备未给血氧。
    """
    device = _upsert_device(session, req.device, req.address, req.timezone)
    save_data_to_file(req.model_dump(), "ppi", req.device)
    for row in req.datas:
        local_time = datetime.strptime(row.time, _TIME_FMT)
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
    return BoomMessage(code=0, msg="")


# --- 10.3 上传睡眠数据（一晚一条摘要）---
@router.get("/sleep", response_model=BoomResponse[list[SleepRow]])
async def get_sleep(
    start: datetime | None = Query(None, description="起始时刻，不传默认近 7 天"),
    end: datetime | None = Query(None, description="结束时刻"),
    skip: int = Query(default=0, ge=0, description="跳过的记录数（分页）"),
    limit: int = Query(default=1000, ge=1, le=10000, description="返回的最大记录数"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[SleepRow]]:
    """10.3 获取睡眠数据（按时间范围）.

    Same time-range / pagination semantics as :func:`get_ppi`, over the
    one-per-night ``Sleep`` summary rows.
    """
    query = select(Sleep)
    if not start:
        start = midnight_days_ago(7)
    if start:
        query = query.where(Sleep.time >= start)
    if end:
        query = query.where(Sleep.time <= end)
    query = query.order_by(col(Sleep.time)).offset(skip).limit(limit)
    rows = session.exec(query).all()
    return BoomResponse(
        code=0,
        data=[
            SleepRow(
                time=r.time.strftime(_TIME_FMT),
                sleepOnsetTime=r.sleep_onset_time,
                awakeTime=r.awake_time,
                lightSleepPeriod=r.light_sleep_period,
                deepSleepPeriod=r.deep_sleep_period,
                otherSleepPeriod=r.other_sleep_period,
                heartRateRest=r.heart_rate_rest,
            )
            for r in rows
        ],
    )


@router.post("/sleep", response_model=BoomMessage)
async def upload_sleep(
    req: SleepUploadRequest, session: Session = Depends(get_session)
) -> BoomMessage:
    """10.3 上传睡眠摘要（入睡/醒来 + 三段时长 + 静息心率）.

    new-interface.md §2: 入睡/醒来及三段时长均为设备原样秒数，画不出逐秒
    曲线，曲线由 ``/upload/ppi`` 提供。
    """
    device = _upsert_device(session, req.device, req.address, req.timezone)
    save_data_to_file(req.model_dump(), "sleep", req.device)
    for row in req.datas:
        local_time = datetime.strptime(row.time, _TIME_FMT)
        stmt = (
            insert(Sleep)
            .values(
                time=local_time,
                timezone=req.timezone,
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
                    "timezone": req.timezone,
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
    return BoomMessage(code=0, msg="")

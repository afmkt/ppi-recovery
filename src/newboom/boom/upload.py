"""10.x 蓝牙数据上传 — ``/upload/ppi`` & ``/upload/sleep``.

The §10 request shapes are superseded by ``docs/new-interface.md``:
``/upload/ppi`` carries the per-second sleep detail (``activity``) plus
``hr`` / ``spo2`` / ``ppi``; ``/upload/sleep`` carries the one-per-night
summary (onset/awake + three stage durations + resting HR). ``spo2`` is the
device raw value (×10); ``spo2 == 0`` means the device gave no sample that
second (the per-second history has no spo2 field, only the broadcast does).

Writes return ``BoomMessage`` (``{code, msg}``); the retrieval GETs return
``BoomResponse[list[…]]``. The functional implementation lives in
:mod:`newboom.upload` (mounted at ``/api/upload``, returns ``ApiResponse``);
this router is the spec-aligned scaffold.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query

from newboom.boom.schemas import BoomMessage, BoomResponse
from newboom.upload import PPIRow, PPIUploadRequest, SleepRow, SleepUploadRequest

router = APIRouter(prefix="/upload", tags=["蓝牙上传"])


# --- 10.1 上传心率数据（逐秒分期曲线 + 心率/血氧/PPI）---
@router.get("/ppi", response_model=BoomResponse[list[PPIRow]])
async def get_ppi(
    start: datetime | None = Query(None, description="起始时刻，不传默认近 7 天"),
    end: datetime | None = Query(None, description="结束时刻"),
    skip: int = Query(default=0, ge=0, description="跳过的记录数（分页）"),
    limit: int = Query(
        default=1000, ge=1, le=10000, description="返回的最大记录数"
    ),
) -> BoomResponse[list[PPIRow]]:
    """10.1 获取 PPI 数据（按时间范围）.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])


@router.post("/ppi", response_model=BoomMessage)
async def upload_ppi(req: PPIUploadRequest) -> BoomMessage:
    """10.1 上传 PPI 数据（逐秒分期曲线 + 心率/血氧/PPI）.

    new-interface.md §3.1: ``spo2`` 为设备原始值(×10)，落库/上传均用原始值，
    仅界面展示时折算百分比；``spo2=0`` 表示该秒设备未给血氧。

    TODO: implement.
    """
    return BoomMessage(code=0, msg="")


# --- 10.3 上传睡眠数据（一晚一条摘要）---
@router.get("/sleep", response_model=BoomResponse[list[SleepRow]])
async def get_sleep(
    start: datetime | None = Query(None, description="起始时刻，不传默认近 7 天"),
    end: datetime | None = Query(None, description="结束时刻"),
    skip: int = Query(default=0, ge=0, description="跳过的记录数（分页）"),
    limit: int = Query(
        default=1000, ge=1, le=10000, description="返回的最大记录数"
    ),
) -> BoomResponse[list[SleepRow]]:
    """10.3 获取睡眠数据（按时间范围）.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])


@router.post("/sleep", response_model=BoomMessage)
async def upload_sleep(req: SleepUploadRequest) -> BoomMessage:
    """10.3 上传睡眠摘要（入睡/醒来 + 三段时长 + 静息心率）.

    new-interface.md §2: 入睡/醒来及三段时长均为设备原样秒数，画不出逐秒
    曲线，曲线由 ``/upload/ppi`` 提供。

    TODO: implement.
    """
    return BoomMessage(code=0, msg="")

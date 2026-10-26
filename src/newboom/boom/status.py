"""2.x 状态页面 — GET /devices/{address}/{status,readiness}."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from newboom.boom.schemas import BoomResponse, DatePoint, TimePoint

router = APIRouter(tags=["状态"])


class StatusData(BaseModel):
    totalStatus: int = Field(0, description="综合状态值 0-100")
    statusProgress: int = Field(0, description="状态进度百分比 0-100")
    hrv: int = Field(0, description="心率变异性 ms（夜间rmssd均值）")
    restingHeartRate: int = Field(0, description="静息心率 bpm")
    hrvData: list[TimePoint] = Field(
        default_factory=list, description="当日HRV变化数据"
    )


@router.get("/devices/{address}/status", response_model=BoomResponse[StatusData])
async def get_status(
    address: str,
    date: str | None = Query(None, description="查询日期 YYYY-MM-DD，不传默认当天"),
) -> BoomResponse[StatusData]:
    """2.1 获取状态页面基础数据.

    TODO: implement.
    """
    return BoomResponse(
        code=0,
        data=StatusData(totalStatus=0, statusProgress=0, hrv=0, restingHeartRate=0),
    )


@router.get(
    "/devices/{address}/readiness", response_model=BoomResponse[list[DatePoint]]
)
async def get_readiness(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
) -> BoomResponse[list[DatePoint]]:
    """2.2 获取训练准备度数据（用状态值替代）.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])

"""5.x 负荷页面 — GET /devices/{address}/load_* / sport_load / sleep_pressure."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from newboom.boom.schemas import BoomResponse, DatePoint, TimePoint

router = APIRouter(tags=["负荷"])


class LoadZone(BaseModel):
    percentage: int = Field(0, description="占比百分比")
    hours: int = Field(0, description="时长（小时）")


class LoadZones(BaseModel):
    overload: LoadZone = Field(
        default_factory=lambda d: LoadZone(**d), description="负荷过载区间"
    )
    warning: LoadZone = Field(
        default_factory=lambda d: LoadZone(**d), description="注意负荷区间"
    )
    normal: LoadZone = Field(
        default_factory=lambda d: LoadZone(**d), description="状态正常区间"
    )
    excellent: LoadZone = Field(
        default_factory=lambda d: LoadZone(**d), description="状态优秀区间"
    )


class HeartRateZoneItems(BaseModel):
    zone0: int = Field(0, description="Zone0 占比 %")
    zone1: int = Field(0, description="Zone1 占比 %")
    zone2: int = Field(0, description="Zone2 占比 %")
    zone3: int = Field(0, description="Zone3 占比 %")
    zone4: int = Field(0, description="Zone4 占比 %")
    zone5: int = Field(0, description="Zone5 占比 %")


class LoadStatusData(BaseModel):
    totalLoad: int = Field(0, description="总负荷值 0-100")
    loadProgress: int = Field(0, description="负荷进度百分比 0-100")
    avgLoad: int = Field(0, description="平均负荷")
    restingHeartRate: int = Field(0, description="静息心率 bpm")
    steps: int = Field(0, description="步数")
    calories: int = Field(0, description="卡路里")
    realtimeLoadData: list[TimePoint] = Field(
        default_factory=list, description="当日实时负荷数据"
    )
    loadZones: LoadZones = Field(default_factory=LoadZones, description="负荷区间状态")
    heartRateZoneData: list[TimePoint] = Field(
        default_factory=list, description="心率区间趋势数据"
    )
    heartRateZoneItems: HeartRateZoneItems = Field(
        default_factory=lambda d: HeartRateZoneItems(**d), description="心率区间分布"
    )


@router.get(
    "/devices/{address}/load_status", response_model=BoomResponse[LoadStatusData]
)
async def get_load_status(
    address: str,
    date: str | None = Query(None, description="查询日期 YYYY-MM-DD，不传默认当天"),
) -> BoomResponse[LoadStatusData]:
    """5.1 获取负荷页面基础数据.

    TODO: implement.
    """
    return BoomResponse(
        code=0,
        data=LoadStatusData(
            totalLoad=0,
            loadProgress=0,
            avgLoad=0,
            restingHeartRate=0,
            steps=0,
            calories=0,
        ),
    )


@router.get(
    "/devices/{address}/load_trend", response_model=BoomResponse[list[DatePoint]]
)
async def get_load_trend(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
) -> BoomResponse[list[DatePoint]]:
    """5.2 获取负荷趋势数据（value = 当日 totalLoad）.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])


@router.get(
    "/devices/{address}/sport_load", response_model=BoomResponse[list[DatePoint]]
)
async def get_sport_load(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
) -> BoomResponse[list[DatePoint]]:
    """5.3 获取运动负荷数据（value = 运动 tss）.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])


@router.get(
    "/devices/{address}/sleep_pressure", response_model=BoomResponse[list[DatePoint]]
)
async def get_sleep_pressure(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
) -> BoomResponse[list[DatePoint]]:
    """5.4 获取睡眠压力数据.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])

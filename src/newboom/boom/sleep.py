"""3.x 睡眠页面 — GET /devices/{address}/sleep_*."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from newboom.boom.schemas import BoomResponse, DatePoint, TimePoint

router = APIRouter(tags=["睡眠"])


class SleepStatusData(BaseModel):
    totalSleep: int = Field(0, description="总睡眠值 0-100")
    sleepProgress: int = Field(0, description="睡眠进度百分比 0-100")
    sleepDuration: str = Field("", description="睡眠时长，如 8小时10分钟")
    sleepQuality: int = Field(0, description="睡眠质量百分比 0-100")
    suggestedSleepDuration: str = Field("", description="建议睡眠时长，如 9:00")
    recoverySleepPercentage: int = Field(0, description="恢复性睡眠当日占比")
    recoverySleepDuration: str = Field("", description="恢复性睡眠总时长，如 4小时50分")
    avgRecoverySleepPercentage: str = Field(
        "", description="恢复性睡眠平均占比，如 52%"
    )
    avgRecoverySleepDuration: str = Field(
        "", description="恢复性睡眠平均时长，如 4小时30分"
    )


class TimeOfDayPoint(BaseModel):
    """Date -> ``HH:mm`` (bedtime / waketime series)."""

    date: str = Field(..., description="日期 YYYY-MM-DD")
    value: str = Field(..., description="时间 HH:mm")


class SleepModeData(BaseModel):
    bedtimes: list[TimeOfDayPoint] = Field(
        default_factory=list, description="每日入睡时间"
    )
    wakeTimes: list[TimeOfDayPoint] = Field(
        default_factory=list, description="每日醒来时间"
    )
    avgBedtime: str = Field("", description="平均入睡时间 HH:mm")
    avgWakeTime: str = Field("", description="平均醒来时间 HH:mm")


@router.get(
    "/devices/{address}/sleep_status", response_model=BoomResponse[SleepStatusData]
)
async def get_sleep_status(
    address: str,
    date: str | None = Query(None, description="查询日期 YYYY-MM-DD，不传默认当天"),
) -> BoomResponse[SleepStatusData]:
    """3.1 获取睡眠页面基础数据.

    TODO: implement.
    """
    return BoomResponse(
        code=0,
        data=SleepStatusData(
            totalSleep=0,
            sleepProgress=0,
            sleepDuration="",
            sleepQuality=0,
            suggestedSleepDuration="",
            recoverySleepPercentage=0,
            recoverySleepDuration="",
            avgRecoverySleepPercentage="",
            avgRecoverySleepDuration="",
        ),
    )


@router.get(
    "/devices/{address}/sleep_metric", response_model=BoomResponse[list[TimePoint]]
)
async def get_sleep_metric(
    address: str,
    date: str = Query(..., description="查询日期 YYYY-MM-DD"),
    metric: str = Query(..., description="指标类型：heartRate/hrv/oxygen"),
) -> BoomResponse[list[TimePoint]]:
    """3.2 获取睡眠期间数据.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])


@router.get(
    "/devices/{address}/sleep_trend", response_model=BoomResponse[list[DatePoint]]
)
async def get_sleep_trend(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
) -> BoomResponse[list[DatePoint]]:
    """3.3 获取睡眠时长趋势数据（value 为小时数）.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])


@router.get("/devices/{address}/sleep_mode", response_model=BoomResponse[SleepModeData])
async def get_sleep_mode(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
) -> BoomResponse[SleepModeData]:
    """3.4 获取睡眠模式数据.

    TODO: implement.
    """
    return BoomResponse(code=0, data=SleepModeData(avgBedtime="", avgWakeTime=""))

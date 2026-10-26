"""1.1 获取首页训练状态 — GET /devices/{address}/go."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from newboom.boom.schemas import BoomResponse

router = APIRouter(tags=["首页"])


class HealthStatus(BaseModel):
    status: int = Field(0, description="综合状态百分比 0-100")
    sleep: int = Field(0, description="睡眠状态百分比 0-100")
    load: int = Field(0, description="负荷状态百分比 0-100")


class HealthCardValues(BaseModel):
    heartRate: int = Field(0, description="当前心率 bpm")
    restingHeartRate: int = Field(0, description="静息心率 bpm")
    oxygen: int = Field(0, description="血氧饱和度 %")
    hrv: int = Field(0, description="心率变异性 ms（上一次设备传输）")


class GoDetails(BaseModel):
    supercompensationTime: str = Field("", description="超量恢复时间段")
    suggestion: str = Field("", description="训练建议（可选）")
    duration: str = Field("", description="建议时长（可选）")
    target: str = Field("", description="训练目标（可选）")


class GoData(BaseModel):
    healthStatus: HealthStatus = Field(default_factory=lambda d: HealthStatus(**d))
    boomGoText: str = Field("", description="BOOM GO 模块文案")
    energyPercentage: int = Field(0, description="储能环形图百分比 0-100")
    healthCardValues: HealthCardValues = Field(
        default_factory=lambda d: HealthCardValues(**d)
    )
    details: GoDetails = Field(default_factory=lambda d: GoDetails(**d))


@router.get("/devices/{address}/go", response_model=BoomResponse[GoData])
async def get_home(address: str) -> BoomResponse[GoData]:
    """1.1 获取首页训练状态.

    TODO: implement — assemble healthStatus / boomGoText / energyPercentage /
    healthCardValues / details for the device.
    """
    return BoomResponse(code=0, data=GoData(boomGoText="", energyPercentage=0))

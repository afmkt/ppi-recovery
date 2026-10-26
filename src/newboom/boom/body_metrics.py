"""7.1 身体指标 — GET /devices/{address}/body_metrics."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from newboom.boom.schemas import BoomResponse, DatePoint

router = APIRouter(tags=["身体指标"])


class BodyMetricsData(BaseModel):
    currentData: str = Field("", description="当前数据值")
    normalRange: str = Field("", description="正常范围")
    trendData: list[DatePoint] = Field(default_factory=list, description="趋势数据列表")
    analysisText: str = Field("", description="分析建议文本")
    updateTime: str = Field("", description="数据更新时间 HH:mm")


@router.get(
    "/devices/{address}/body_metrics", response_model=BoomResponse[BodyMetricsData]
)
async def get_body_metrics(
    address: str,
    metric: str = Query(
        ..., description="指标类型：heartRate/restingHeartRate/bloodOxygen/hrv"
    ),
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
) -> BoomResponse[BodyMetricsData]:
    """7.1 获取身体指标数据.

    TODO: implement.
    """
    return BoomResponse(
        code=0,
        data=BodyMetricsData(
            currentData="", normalRange="", analysisText="", updateTime=""
        ),
    )

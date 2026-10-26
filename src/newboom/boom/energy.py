"""4.x 储能页面 — GET /devices/{address}/energy_*."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from newboom.boom.schemas import BoomResponse, DatePoint, TimePoint

router = APIRouter(tags=["储能"])


class EnergyStatusData(BaseModel):
    totalEnergy: int = Field(0, description="总储能值 0-100")
    energyProgress: int = Field(0, description="储能进度百分比 0-100")
    totalCharge: int = Field(0, description="总充能值")
    totalConsume: int = Field(0, description="总耗能值")
    energyChartData: list[TimePoint] = Field(
        default_factory=list, description="当日能量曲线数据"
    )


@router.get(
    "/devices/{address}/energy_status", response_model=BoomResponse[EnergyStatusData]
)
async def get_energy_status(
    address: str,
    date: str | None = Query(None, description="查询日期 YYYY-MM-DD，不传默认当天"),
) -> BoomResponse[EnergyStatusData]:
    """4.1 获取储能页面基础数据.

    TODO: implement.
    """
    return BoomResponse(
        code=0,
        data=EnergyStatusData(
            totalEnergy=0, energyProgress=0, totalCharge=0, totalConsume=0
        ),
    )


@router.get(
    "/devices/{address}/energy_trend", response_model=BoomResponse[list[DatePoint]]
)
async def get_energy_trend(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
) -> BoomResponse[list[DatePoint]]:
    """4.2 获取身体储能趋势数据.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])

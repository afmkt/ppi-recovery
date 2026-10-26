"""6.x 超量恢复页面 — GET /devices/{address}/{superrecovery,atlctl}."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from newboom.boom.schemas import BoomResponse

router = APIRouter(tags=["超量恢复"])


class SuperRecoveryData(BaseModel):
    tsb: int = Field(0, description="TSB 训练状态值")
    atl: int = Field(0, description="短期负荷值")
    ctl: int = Field(0, description="长期负荷值")


class AtlCtlPoint(BaseModel):
    date: str = Field(..., description="日期 YYYY-MM-DD")
    atl: int = Field(..., description="短期负荷值")
    ctl: int = Field(..., description="长期负荷值")


@router.get(
    "/devices/{address}/superrecovery", response_model=BoomResponse[SuperRecoveryData]
)
async def get_superrecovery(address: str) -> BoomResponse[SuperRecoveryData]:
    """6.1 获取超量恢复基础数据.

    TODO: implement.
    """
    return BoomResponse(code=0, data=SuperRecoveryData(tsb=0, atl=0, ctl=0))


@router.get("/devices/{address}/atlctl", response_model=BoomResponse[list[AtlCtlPoint]])
async def get_atlctl(
    address: str,
    startDate: str = Query(..., description="开始日期 YYYY-MM-DD"),
    endDate: str = Query(..., description="结束日期 YYYY-MM-DD"),
) -> BoomResponse[list[AtlCtlPoint]]:
    """6.2 获取 ATL/CTL 趋势数据.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])

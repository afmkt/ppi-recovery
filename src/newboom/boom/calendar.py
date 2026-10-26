"""8.1 日历图表 — GET /devices/{address}/calendar_tables."""

from __future__ import annotations

from fastapi import APIRouter, Query

from newboom.boom.schemas import BoomResponse, TimePoint

router = APIRouter(tags=["日历"])


@router.get(
    "/devices/{address}/calendar_tables", response_model=BoomResponse[list[TimePoint]]
)
async def get_calendar_tables(
    address: str,
    date: str = Query(..., description="查询日期 YYYY-MM-DD"),
    tab: str = Query(..., description="图表类型：energy/load/status/sleep/review"),
) -> BoomResponse[list[TimePoint]]:
    """8.1 获取日历图表数据.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])

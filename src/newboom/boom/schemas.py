"""Shared pydantic schemas for the BOOM (v1.0) API surface.

Every BOOM endpoint wraps its payload in one of two envelopes:

* ``BoomResponse`` — ``{code, data}`` for the read endpoints.
* ``BoomMessage``  — ``{code, msg}`` for the write / void endpoints.

Chart endpoints reuse ``TimePoint`` (``HH:mm -> value``) and ``DatePoint``
(``YYYY-MM-DD -> value``). Endpoint-specific payloads live alongside their
route in the per-page modules.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class BoomResponse[T](BaseModel):
    """``{code, data}`` envelope for read endpoints. ``code`` is 0 on success."""

    code: int = Field(0, description="响应代码, 0=成功")
    data: T | None = Field(None, description="端点载荷")


class BoomMessage(BaseModel):
    """``{code, msg}`` envelope for write / void endpoints."""

    code: int = Field(0, description="响应代码, 0=成功")
    msg: str = Field("", description="错误信息")


class TimePoint(BaseModel):
    """Intraday chart point: ``HH:mm`` -> numeric value."""

    time: str = Field(..., description="时间点 HH:mm")
    value: float = Field(..., description="指标值")


class StaleFields(BaseModel):
    """Shared fields on every *current-card* payload (``/go``, ``/status``,
    ``/energy_status``, ``/load_status``, ``/superrecovery``, ``/body_metrics``).

    ``asOf`` is the date the value came from (``None`` when the device has no
    data at all); ``stale`` is True when that date is older than
    ``STALE_WINDOW_DAYS`` (device-local) or when there is no data. The value is
    always returned ("mark, don't cap") — the marker is what lets the UI dim or
    label a value that may be days old. See :mod:`newboom.boom.staleness`."""

    asOf: str | None = Field(
        default=None,
        description="数据日期 YYYY-MM-DD; null = 该设备尚无任何数据",
    )
    stale: bool = Field(
        default=False,
        description="True 当 asOf 超过陈旧窗口或无数据（值仍返回，仅标记）",
    )


class DatePoint(BaseModel):
    """Daily trend point: ``YYYY-MM-DD`` -> numeric value.

    ``carried`` marks a LOCF-filled point: a day with no real value that is
    filled by the last real value within ``STALE_WINDOW_DAYS`` (see
    :mod:`newboom.boom.staleness`). A gap larger than the window is omitted
    rather than fabricated, so a ``carried`` point is always a real, marked
    backfill — never a silent zero.
    """

    date: str = Field(..., description="日期 YYYY-MM-DD")
    value: float = Field(..., description="指标值")
    carried: bool = Field(
        default=False,
        description="True when this day has no real value and is a LOCF backfill "
        "of the last real value within the staleness window",
    )


class AtlCtlPoint(BaseModel):
    """Daily ``atl``/``ctl`` pair trend point (``/atlctl``). ``carried`` marks a
    LOCF backfill of the last real pair within the staleness window (both fields
    are carried together), same contract as :class:`DatePoint.carried`."""

    date: str = Field(..., description="日期 YYYY-MM-DD")
    atl: int = Field(..., description="短期负荷值")
    ctl: int = Field(..., description="长期负荷值")
    carried: bool = Field(
        default=False,
        description="True when this day has no real atl/ctl and is a LOCF backfill "
        "of the last real pair within the staleness window",
    )

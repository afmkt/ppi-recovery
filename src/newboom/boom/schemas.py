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


class DatePoint(BaseModel):
    """Daily trend point: ``YYYY-MM-DD`` -> numeric value."""

    date: str = Field(..., description="日期 YYYY-MM-DD")
    value: float = Field(..., description="指标值")

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ApiResponse(BaseModel):
    ok: bool
    message: str | None = None
    device_id: int | None = None
    processed: int | None = None
    file: str | None = None
    data: Any | None = None

"""Shared schema primitives: errors, pagination, health."""

from datetime import datetime, timezone
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """Canonical error envelope returned by every non-2xx response."""

    error: ErrorBody


class PaginationParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)


class Paginated(BaseModel, Generic[T]):
    items: list[T]
    total: int = 0
    page: int = 1
    page_size: int = 20


class HealthResponse(BaseModel):
    status: str = "ok"


class ComponentHealth(BaseModel):
    status: str  # "ok" | "degraded" | "down" | "disabled"
    detail: str | None = None


class SystemHealth(BaseModel):
    status: str = "ok"
    version: str
    app_env: str
    simulation_mode: bool
    time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    checks: dict[str, ComponentHealth] = Field(default_factory=dict)


class TimeSeriesPoint(BaseModel):
    ts: datetime
    value: float

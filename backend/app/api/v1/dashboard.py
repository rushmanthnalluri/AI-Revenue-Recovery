"""Dashboard endpoints. Owner: dashboard/frontend agent."""

from typing import Literal

from fastapi import APIRouter, Query

from app.schemas.dashboard import DashboardSummary, DashboardTimeseries

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
def get_summary() -> DashboardSummary:
    # Stub: zeroed summary until the dashboard agent wires real rollups.
    return DashboardSummary()


@router.get("/timeseries", response_model=DashboardTimeseries)
def get_timeseries(
    metric: str = Query(default="payment_success_rate"),
    granularity: Literal["minute", "hour", "day"] = Query(default="hour"),
    window_hours: int = Query(default=24, ge=1, le=24 * 30),
) -> DashboardTimeseries:
    return DashboardTimeseries(metric=metric, granularity=granularity, points=[])

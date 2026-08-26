"""Dashboard schemas — executive summary and metric time series."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import TimeSeriesPoint


class DashboardSummary(BaseModel):
    currency: str = "INR"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    open_incidents: int = 0
    incidents_by_severity: dict[str, int] = Field(default_factory=dict)
    revenue_at_risk_paise: int = 0
    recovered_revenue_paise: int = 0
    lost_revenue_paise: int = 0
    recovery_rate: float = 0.0  # recovered / (recovered + lost + at_risk)
    active_recoveries: int = 0
    payments_success_rate: float = 0.0
    payments_observed: int = 0
    pending_approvals: int = 0


class DashboardTimeseries(BaseModel):
    metric: str  # payment_success_rate | revenue_at_risk_paise | recovered_revenue_paise | ...
    granularity: Literal["minute", "hour", "day"] = "hour"
    currency: str = "INR"
    points: list[TimeSeriesPoint] = Field(default_factory=list)

"""Dashboard schemas — executive summary and metric time series."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import TimeSeriesPoint
from app.schemas.incidents import IncidentSummary


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
    # --- additive (backwards compatible) ---
    # Success rate over the baseline window immediately preceding the current
    # window; None when the baseline has no observations.
    payments_baseline_success_rate: float | None = None
    # Recoverable share of the at-risk loss (counterfactual x recoverability).
    recoverable_revenue_paise: int = 0
    # True when any open incident's at-risk point estimate is low-confidence
    # (thin baseline) — the headline number should be read with its band.
    revenue_at_risk_low_confidence: bool = False
    # Newest incidents by detected_at (max 5) for the dashboard feed.
    recent_incidents: list[IncidentSummary] = Field(default_factory=list)


class DashboardTimeseries(BaseModel):
    metric: str  # payment_success_rate | capture_latency_ms | payments_total |
    # payments_failed | failed_amount_paise | recovered_revenue_paise
    granularity: Literal["minute", "hour", "day"] = "hour"
    currency: str = "INR"
    points: list[TimeSeriesPoint] = Field(default_factory=list)

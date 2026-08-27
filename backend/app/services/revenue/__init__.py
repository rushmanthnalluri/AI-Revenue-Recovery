"""Revenue-at-risk engine.

Quantifies what a payment incident actually costs and what recovery is worth,
using a counterfactual methodology — never equating failed transactions with
lost revenue. See docs/revenue-methodology.md for the full methodology.

Four distinct numbers are produced, and they are never interchangeable:
- observed_loss:      counterfactual expected revenue minus actual captured.
- recoverable:        the share of observed_loss that is realistically
                      winnable, via per-failure-class recoverability factors.
- expected_recovery:  recoverable discounted by per-strategy effectiveness
                      priors (planning number for the strategy generator).
- actual_recovered:   measured, webhook-verified captures attributed through
                      recovery_actions. Not an estimate — no sampling band.
"""

from app.services.revenue.classify import FailureClass, classify_failure, classify_reason
from app.services.revenue.config import DEFAULT_CONFIG, RevenueConfig
from app.services.revenue.engine import RevenueService
from app.services.revenue.statistics import rate_confidence, wilson_interval
from app.services.revenue.types import (
    Estimate,
    FailureClassBreakdown,
    OpportunityEstimate,
    RecoveredRevenueReport,
    RevenueAtRiskReport,
    SegmentBreakdown,
)

__all__ = [
    "DEFAULT_CONFIG",
    "Estimate",
    "FailureClass",
    "FailureClassBreakdown",
    "OpportunityEstimate",
    "RecoveredRevenueReport",
    "RevenueAtRiskReport",
    "RevenueConfig",
    "RevenueService",
    "SegmentBreakdown",
    "classify_failure",
    "classify_reason",
    "rate_confidence",
    "wilson_interval",
]

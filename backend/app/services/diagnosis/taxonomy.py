"""Root-cause label taxonomy for incident diagnosis.

Aligned with the simulator's injection taxonomy (docs/research.md, ADR 0005) so
that diagnoses can be scored against simulator ground truth. The enum order is
the canonical class order used everywhere (metrics, confusion matrices, proba
vectors).
"""

from enum import Enum


class CauseLabel(str, Enum):
    # Infrastructure / Razorpay-side
    GATEWAY_DEGRADATION = "gateway_degradation"
    ROUTE_LATENCY = "route_latency"
    METHOD_OUTAGE = "method_outage"
    BANK_DOWNTIME = "bank_downtime"
    # Customer-intent / merchant-side
    ABANDONMENT_SPIKE = "abandonment_spike"
    SUBSCRIPTION_FAILURE_SPIKE = "subscription_failure_spike"
    CUSTOMER_INSUFFICIENT_FUNDS_WAVE = "customer_insufficient_funds_wave"
    # Detection fired but nothing is actually wrong
    NO_FAULT = "no_fault"


#: Canonical ordered list of label strings.
CAUSES: list[str] = [c.value for c in CauseLabel]

__all__ = ["CauseLabel", "CAUSES"]

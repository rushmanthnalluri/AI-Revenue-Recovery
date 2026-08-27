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

#: Causes whose injected failure mix is dominated by *timeout / soft-decline*
#: failures — the classes where an automated retry / route-around is the
#: sanctioned remediation, so a confident diagnosis may enter the policy
#: auto-execute lane (strategy confidence = diagnosis confidence x action-fit,
#: gated at 0.85 by ``policies/default.yaml``). Derivation, from the
#: simulator's forced reason mix (``app/simulator/incidents.py::_FAIL_REASONS``)
#: mapped through the recovery failure classes
#: (``app/services/revenue/classify.py::_REASON_PATTERNS``):
#:
#: - gateway_degradation: 0.80 gateway_technical_error (SOFT_DECLINE) +
#:   0.20 payment_timed_out (TIMEOUT) -> 100% timeout/soft. AUTO.
#: - method_outage / bank_downtime: 0.70 bank_downtime (error_source=bank ->
#:   SOFT_DECLINE fallback) + 0.30 bank_technical_error (SOFT_DECLINE)
#:   -> 100% soft. AUTO.
#: - route_latency: NOT auto — its defining effect is latency on *captured*
#:   payments (fail_boost ~0.06 incidental timeouts); customer-facing
#:   recovery automation on slow-but-successful payments is not sanctioned
#:   (the simulator's own expected_truth marks it recoverable=False).
#: - subscription_failure_spike: 0.50 insufficient_fund + 0.30 payment_declined
#:   + 0.20 payment_timed_out -> timeout/soft share exactly 0.50, not a
#:   majority; remediation is dunning with delay. NOT auto.
#: - customer_insufficient_funds_wave: 0.10 timeout/soft. NOT auto.
#: - abandonment_spike: ~0 (abandonment class). NOT auto.
#: - no_fault: nothing to recover; any confident cause is the unsafe side.
AUTO_RECOVERABLE_CAUSES: frozenset[str] = frozenset(
    {
        CauseLabel.GATEWAY_DEGRADATION.value,
        CauseLabel.METHOD_OUTAGE.value,
        CauseLabel.BANK_DOWNTIME.value,
    }
)

__all__ = ["CauseLabel", "CAUSES", "AUTO_RECOVERABLE_CAUSES"]

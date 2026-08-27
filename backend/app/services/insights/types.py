"""Typed results of the decline-outlier computation.

Rates and lifts are rounded at construction time (rates 6dp, lift 4dp) and the
ranking sorts on those rounded values, so the numbers shown are exactly the
numbers that were ranked — no hidden precision can reorder ties.
"""

from dataclasses import dataclass, field
from datetime import datetime

# Facet bases. Group facets exist on every payment (method/bank/gateway), so
# the honest measure is the within-group failure RATE. Error facets exist only
# on failures (error_code/error_reason), where within-group rates are
# meaningless, so the measure is the SHARE of all failures.
BASIS_FAILURE_RATE = "failure_rate"
BASIS_FAILURE_SHARE = "failure_share"

# Platform-callout classifications.
CLASS_PLATFORM_WIDE = "platform_wide"
CLASS_INCIDENT_SPECIFIC = "incident_specific"

# Honest scope label: this deployment is the single-merchant simulator, so the
# "platform" benchmark is the simulator fleet (= this merchant's full payment
# stream). In a multi-merchant deployment this would benchmark all merchants.
SCOPE_SIMULATED_FLEET = "simulated_fleet"


@dataclass(frozen=True)
class FacetOutlier:
    """One failure facet overrepresented in the incident window vs baseline."""

    dimension: str  # method | bank | gateway | error_code | error_reason
    value: str
    basis: str  # BASIS_FAILURE_RATE | BASIS_FAILURE_SHARE
    incident_rate: float  # within-group failure rate, or share of window failures
    baseline_rate: float  # same measure over the baseline window
    # incident_rate / baseline_rate; None when the baseline rate is zero —
    # the facet was absent at baseline ("new"), which ranks above any finite lift.
    lift: float | None
    support: int  # failures attributed to this facet inside the incident window
    window_group_size: int  # denominator behind incident_rate
    baseline_group_size: int  # denominator behind baseline_rate
    low_confidence: bool  # support below the confident-support floor


@dataclass(frozen=True)
class PlatformCallout:
    """Merchant-vs-network benchmark of the top-ranked outlier (Pagos pattern)."""

    dimension: str
    value: str
    classification: str  # CLASS_PLATFORM_WIDE | CLASS_INCIDENT_SPECIFIC
    platform_scope: str  # SCOPE_SIMULATED_FLEET in this deployment
    platform_window_rate: float
    platform_baseline_rate: float
    platform_lift: float | None
    platform_support: int
    summary: str


@dataclass(frozen=True)
class ComputedFrom:
    """Provenance: every number above is recomputable from these windows."""

    window_start: datetime
    window_end: datetime
    baseline_start: datetime
    baseline_end: datetime
    segment: dict[str, str] = field(default_factory=dict)
    window_payments: int = 0
    window_failures: int = 0
    baseline_payments: int = 0
    baseline_failures: int = 0


@dataclass(frozen=True)
class IncidentInsights:
    outliers: list[FacetOutlier]
    platform_callout: PlatformCallout | None
    computed_from: ComputedFrom

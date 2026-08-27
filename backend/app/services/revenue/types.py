"""Result types for the revenue-at-risk engine.

Everything is integer paise + "INR" per the repo money convention. Estimates
always carry an uncertainty band and a confidence score; a point estimate of
`None` means "no defensible point exists — use the band" (zero-signal case).
These are plain dataclasses: HTTP serialization is a later-wave concern.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Estimate:
    """A paise estimate with an uncertainty band.

    - point_paise: the single best number, or None when the evidence is too
      thin for any point to be defensible (band still populated).
    - lower_paise / upper_paise: uncertainty band, propagated from a Wilson
      score interval on the underlying rate (see statistics.py).
    - confidence: 0..1, driven by sample size behind the underlying rate.
    - low_confidence: explicit "do not trust the point" flag for consumers.
    - basis: short human-readable note on how the number was derived.
    """

    point_paise: int | None
    lower_paise: int
    upper_paise: int
    confidence: float
    low_confidence: bool
    basis: str = ""

    @classmethod
    def zero(cls, basis: str, *, confidence: float = 0.0) -> "Estimate":
        """A measured zero (nothing observed), still honestly flagged."""
        return cls(
            point_paise=0,
            lower_paise=0,
            upper_paise=0,
            confidence=confidence,
            low_confidence=confidence < 0.5,
            basis=basis,
        )

    def scale(self, factor: float, basis: str | None = None) -> "Estimate":
        """Multiply by a deterministic factor (e.g. a recoverability prior).

        Bands are widened outward (floor lower / ceil upper) so scaling never
        makes the band falsely narrower in paise terms.
        """
        import math

        return Estimate(
            point_paise=None if self.point_paise is None else int(round(self.point_paise * factor)),
            lower_paise=int(math.floor(self.lower_paise * factor)),
            upper_paise=int(math.ceil(self.upper_paise * factor)),
            confidence=self.confidence,
            low_confidence=self.low_confidence,
            basis=self.basis if basis is None else basis,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SegmentBreakdown:
    """Counterfactual loss for one (method x amount-band x customer-type) cell."""

    segment_key: str
    method: str
    amount_band: str
    customer_type: str  # "new" | "returning" | "unknown"
    attempted_count: int  # resolved attempts in the incident window
    failed_count: int
    captured_count: int
    attempted_amount_paise: int
    captured_amount_paise: int
    baseline_n: int
    baseline_success_rate: float | None
    baseline_rate_ci: tuple[float, float]
    avg_order_value_paise: int
    counterfactual_expected_paise: int | None
    observed_loss: Estimate

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FailureClassBreakdown:
    """Observed loss allocated to one failure class, and its recoverable share."""

    failure_class: str
    failed_count: int
    failed_amount_paise: int
    allocated_loss: Estimate
    recoverability_factor: float
    recoverable: Estimate

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RevenueAtRiskReport:
    """Full counterfactual analysis for one incident.

    observed_loss, recoverable, expected_recovery (per strategy) and
    actual_recovered are four distinct numbers — see module docstring.
    """

    incident_id: str
    currency: str
    window_start: datetime
    window_end: datetime
    baseline_start: datetime
    baseline_end: datetime
    observed_loss: Estimate
    recoverable: Estimate
    expected_recovery_by_strategy: dict[str, Estimate]
    actual_recovered_paise: int  # measured, verified — not an estimate
    recovered_actions_count: int
    segments: list[SegmentBreakdown] = field(default_factory=list)
    failure_classes: list[FailureClassBreakdown] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpportunityEstimate:
    """Planning estimate for a single recovery opportunity.

    Per-opportunity numbers are prior-driven (a single payment is a Bernoulli
    outcome), so bands are wide by construction and low_confidence is always
    True — meaningful intervals only exist over populations.
    """

    opportunity_id: str
    amount_paise: int
    currency: str
    failure_class: str
    failure_class_source: str  # "payment" | "opportunity_type_default"
    recoverability_factor: float
    recoverable: Estimate
    expected_recovery_by_strategy: dict[str, Estimate]
    recommended_action_type: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecoveredRevenueReport:
    """Measured recovered revenue over a window (dashboard number).

    Only recovery_actions in status RECOVERED (webhook-verified) count.
    UNKNOWN outcomes are surfaced separately, never silently included.
    """

    window_start: datetime
    window_end: datetime
    currency: str
    total_recovered_paise: int
    recovered_actions_count: int
    unknown_actions_count: int  # executed but unverifiable — excluded on purpose
    by_incident: dict[str, int]
    by_action_type: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

"""Randomized-holdout assignment and incremental-lift statistics.

Pre-registered design (docs/product-strategy.md §4.1/§7; results and
methodology in docs/evaluation.md):

- Within the PULSECOVER arm, a customer-level DETERMINISTIC holdout receives
  NO PulseRecover recovery actions: opportunities (and therefore actions) are
  never built for held-out customers. Detection and diagnosis still run
  fleet-wide — the holdout withholds only the *intervention*, which is what a
  real no-action control group means.
- Membership is a pure function of the run seed and the customer id, so two
  runs with the same seed assign identical groups and produce identical
  metrics. Nothing about membership depends on wall clock, platform, or
  iteration order.
- Estimand: incremental lift = recovery_rate(treatment) −
  recovery_rate(holdout) over the run's fixed attribution window.
- Statistics: difference of two proportions with a Newcombe hybrid score
  (Wilson) confidence interval — closed form, deterministic, and well
  behaved at tiny counts: small groups yield honestly wide bands, never a
  bare point estimate.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.services.recovery import OpportunityBuilder

# Default share of customers randomized into the no-action holdout
# (docs/product-strategy.md §4.1: 5–10%).
DEFAULT_HOLDOUT_FRACTION = 0.10

# Two-sided 95% confidence for the Wilson/Newcombe intervals below.
CI_LEVEL = 0.95
CI_Z = 1.959963985


# ---------------------------------------------------------------------------
# deterministic assignment
# ---------------------------------------------------------------------------


def holdout_token(seed: int, customer_id: str) -> float:
    """Stable assignment uniform in [0, 1) for a (run seed, customer id) pair.

    SHA-256 over the namespaced pair, first 8 digest bytes as an unsigned
    big-endian integer over 2**64. Platform- and language-independent, so
    membership is reproducible from the stored seed + fraction alone.
    """
    digest = hashlib.sha256(f"holdout:{seed}:{customer_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def is_holdout(seed: int, fraction: float, customer_id: str | None) -> bool:
    """Customer-level holdout membership. Payments without a customer id
    cannot be randomized at customer level and stay in the treatment group
    (a disclosed assignment rule, not a silent drop)."""
    if customer_id is None or fraction <= 0.0:
        return False
    return holdout_token(seed, customer_id) < fraction


# ---------------------------------------------------------------------------
# confidence intervals (closed form, tiny-count safe)
# ---------------------------------------------------------------------------


def wilson_interval(successes: int, n: int, z: float = CI_Z) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Degenerate inputs stay honest instead of crashing: ``n == 0`` → (0, 1)
    (total ignorance); ``successes == 0`` → lower bound exactly 0 with a
    wide upper bound; ``successes == n`` → upper bound exactly 1.
    """
    if n <= 0:
        return (0.0, 1.0)
    z2 = z * z
    p = successes / n
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


def newcombe_ci(
    treat_ok: int, treat_n: int, hold_ok: int, hold_n: int, z: float = CI_Z
) -> tuple[float, float]:
    """Newcombe hybrid score interval (method 10, Newcombe 1998) for the
    difference p_treatment − p_holdout: per-group Wilson intervals combined
    without pooling. Deterministic; tiny counts produce wide bands that
    always bracket the point estimate."""
    p1 = treat_ok / treat_n if treat_n else 0.0
    p2 = hold_ok / hold_n if hold_n else 0.0
    l1, u1 = wilson_interval(treat_ok, treat_n, z)
    l2, u2 = wilson_interval(hold_ok, hold_n, z)
    delta = p1 - p2
    low = delta - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    high = delta + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return (max(-1.0, low), min(1.0, high))


def median(values: list[float]) -> float | None:
    """Median of a sample; None when empty (reported as 'no observations')."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


# ---------------------------------------------------------------------------
# holdout-aware opportunity selection
# ---------------------------------------------------------------------------


class HoldoutExcludingBuilder(OpportunityBuilder):
    """OpportunityBuilder with the holdout applied: customers randomized into
    the no-action holdout never get opportunities — and therefore never
    recovery actions. Detection and diagnosis upstream are unaffected; only
    the recovery loop's work selection changes."""

    def __init__(
        self, session: Session, *, is_excluded: Callable[[str | None], bool]
    ) -> None:
        super().__init__(session)
        self._is_excluded = is_excluded

    def _failed_payments(self, start, end, source_types):  # noqa: ANN001 - parent signature
        return [
            p
            for p in super()._failed_payments(start, end, source_types)
            if not self._is_excluded(p.customer_id)
        ]

    def _abandoned_orders(self, start, end, source_types):  # noqa: ANN001 - parent signature
        return [
            o
            for o in super()._abandoned_orders(start, end, source_types)
            if not self._is_excluded(o.customer_id)
        ]

    def _stuck_created_payments(self, start, end, knowledge_edge, source_types):  # noqa: ANN001 - parent signature
        return [
            p
            for p in super()._stuck_created_payments(start, end, knowledge_edge, source_types)
            if not self._is_excluded(p.customer_id)
        ]

    def _stuck_subscriptions(self, source_types):  # noqa: ANN001 - parent signature
        return [
            s
            for s in super()._stuck_subscriptions(source_types)
            if not self._is_excluded(s.customer_id)
        ]


__all__ = [
    "CI_LEVEL",
    "CI_Z",
    "DEFAULT_HOLDOUT_FRACTION",
    "HoldoutExcludingBuilder",
    "holdout_token",
    "is_holdout",
    "median",
    "newcombe_ci",
    "wilson_interval",
]

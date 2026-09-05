"""Measured action-outcome aggregation service (Phase B, C1 + C4).

Closes the read-only evidence vertex of the closed learning loop:

    intervention -> outcome -> evidence -> future-decision

For each (action_type x failure_class) cell, computed per environment from the
real executor path (terminal ``recovery_actions`` rows, webhook-verified where
applicable), we report:

- raw conversion: ``n_recovered / n_executed`` with a Wilson score CI;
- organic baseline: payments that ultimately captured *without* any
  ``RECOVERED`` action linked to them (``compute_organic_rates``);
- incremental lift: the difference ``P(recovered | action, class) - P(organic
  | class)`` with a Newcombe CI; labeled ``inconclusive`` whenever the CI
  brackets zero so downstream consumers retain the prior instead of acting
  on noise.

Provenance is hard-coded to ``measured_from_action_outcomes`` so the read
side can distinguish these rates from harness ``OutcomeModel`` rates
(rejected per learning-loop.md C7 — importing simulator behavior into live
decisioning is prior laundering) and from the documented
``RevenueConfig`` priors.

Environment isolation is enforced by ``RecoveryAction.environment``; the
organic baseline further narrows to the ``source_type``s that belong to the
same environment (``source_types_for_environment``). The two-sided 95%
confidence level matches the rest of the repo (Wilson z = 1.96, Newcombe z
= 1.959963985 in ``app.services.evaluation.holdout``).

This service is read-only — it never mutates ``recovery_actions`` or
``payments``. Its outputs are evidence-only today; strategy ranking and the
policy gate remain on their documented prior/configuration paths until a
separately gated decision-support slice is proven.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models.base import source_types_for_environment
from app.models.commerce import Payment
from app.models.learning import RecoveryOutcomeObservation
from app.models.recovery import PolicyDecisionRecord, RecoveryAction, RecoveryOpportunity
from app.ports import ActionType, RecoveryStatus
from app.services.revenue.classify import FailureClass, classify_failure
from app.services.revenue.config import DEFAULT_CONFIG, RevenueConfig
from app.services.revenue.statistics import rate_confidence, wilson_interval


# Sample-size gate for honest measured rates, matching the outcome-harness
# precedent (app/services/evaluation/outcomes.py::MIN_CELL = 30). Below this,
# the cell is reported but flagged ``low_confidence=True`` so downstream
# consumers retain the prior path.
MIN_CELL = 30

# Sample size at which the linear confidence ramp saturates to 1.0
# (app/services/revenue/config.py::full_confidence_sample = 200).
FULL_CONFIDENCE_SAMPLE = 200

# Provenance label stamped on every result this module produces.
PROVENANCE = "measured_from_action_outcomes"

# Wilson z mirrored from app/services/revenue/config.py (two-sided 95%).
WILSON_Z = 1.96

OBSERVED_OUTCOME_STATUSES = frozenset(
    {RecoveryStatus.RECOVERED, RecoveryStatus.FAILED, RecoveryStatus.UNKNOWN}
)


def record_outcome_observation(
    db,
    action: RecoveryAction,
    status: RecoveryStatus,
    *,
    source: str,
    observed_at: datetime | None = None,
    evidence: dict | None = None,
) -> RecoveryOutcomeObservation | None:
    """Persist the first observation of one action outcome state.

    Duplicate webhooks, reconciliation, and repeated verification are no-ops
    because the database uniqueness key is ``(action_id, observed_status)``.
    """
    if status not in OBSERVED_OUTCOME_STATUSES:
        return None
    existing = db.scalar(
        sa.select(RecoveryOutcomeObservation).where(
            RecoveryOutcomeObservation.action_id == action.id,
            RecoveryOutcomeObservation.observed_status == status,
        )
    )
    if existing is not None:
        return existing

    decision = (
        db.get(PolicyDecisionRecord, action.policy_decision_id)
        if action.policy_decision_id
        else None
    )
    decision_at = action.decided_at or action.proposed_at
    observation = RecoveryOutcomeObservation(
        action_id=action.id,
        opportunity_id=action.opportunity_id,
        action_type=action.action_type,
        observed_status=status,
        decision_at=decision_at,
        observed_at=observed_at or utcnow(),
        policy_decision_id=action.policy_decision_id,
        policy_version=decision.policy_version if decision else None,
        gateway_request_id=action.gateway_request_id,
        source=source,
        evidence=dict(evidence or {}),
        environment=action.environment or "research",
    )
    db.add(observation)
    db.flush()
    return observation


@dataclass(frozen=True)
class ActionOutcomeCell:
    """One measured (action_type x failure_class) cell.

    All rates live in [0, 1]; ``n_executed`` is the terminal-action
    denominator (RECOVERED + FAILED; UNKNOWN is *counted separately*, never
    silently folded into the denominator). ``low_confidence`` is True when
    n_executed < MIN_CELL or when n_executed == 0 — same convention as the
    rest of the revenue engine.
    """

    environment: str
    action_type: ActionType
    failure_class: FailureClass
    failure_class_source: str  # "payment" | "opportunity_type_default" | "no_signal"
    n_executed: int
    n_recovered: int
    n_failed: int
    n_unknown: int
    rate_recovered: float | None  # None when denominator == 0 (no defensible point)
    wilson_low: float
    wilson_high: float
    low_confidence: bool
    sample_confidence: float  # min(1, denom / FULL_CONFIDENCE_SAMPLE)

    def basis(self) -> str:
        """Honest one-line provenance for this cell (UI / audit)."""
        denom = self.n_recovered + self.n_failed
        if denom == 0:
            return (
                f"no terminal actions in window; provenance={PROVENANCE}; "
                f"class={self.failure_class.value} ({self.failure_class_source})"
            )
        ci = f"[{self.wilson_low:.4f}, {self.wilson_high:.4f}]"
        conf = "low" if self.low_confidence else "measured"
        return (
            f"{conf}: n={denom}, rate={self.rate_recovered}, "
            f"Wilson{ci}; provenance={PROVENANCE}; "
            f"class={self.failure_class.value} ({self.failure_class_source}); "
            f"env={self.environment}"
        )


@dataclass(frozen=True)
class IncrementalRate:
    """P(recovered | action, class) - P(organic | class), Newcombe CI.

    ``incremental`` is the *signed* value — it may be negative when the
    measured action rate is below the organic baseline. ``clamped`` is the
    same value floored at 0 for ranking consumers that must remain
    non-negative; the signed value is always available so the UI can display
    "we ranked by attribution? no — by incremental recovery".
    """

    action_type: ActionType
    failure_class: FailureClass
    action_rate: float | None  # None when action cell has n == 0
    organic_rate: float | None  # None when organic cell has n == 0
    incremental: float | None
    clamped: float  # max(incremental, 0); 0.0 when inputs are None
    ci_low: float
    ci_high: float
    inconclusive: bool  # CI brackets zero — consumer must fall back to prior

    def basis(self) -> str:
        if self.action_rate is None or self.organic_rate is None:
            return (
                f"insufficient data: action_rate={self.action_rate}, "
                f"organic_rate={self.organic_rate}; inconclusive"
            )
        ci = f"[{self.ci_low:.4f}, {self.ci_high:.4f}]"
        verdict = "inconclusive (CI brackets 0)" if self.inconclusive else "measurable"
        return (
            f"incremental = action_rate({self.action_rate:.4f}) - "
            f"organic({self.organic_rate:.4f}) = {self.incremental:.4f}; "
            f"Newcombe{ci}; {verdict}"
        )


@dataclass(frozen=True)
class OrganicRateCell:
    """One (failure_class) organic cell — payments that self-captured
    without any linked RECOVERED action in the same window.
    """

    environment: str
    failure_class: FailureClass
    n_failed_payments: int
    n_self_captured: int
    rate_organic: float | None
    wilson_low: float
    wilson_high: float
    low_confidence: bool
    sample_confidence: float

    def basis(self) -> str:
        if self.n_failed_payments == 0:
            return (
                f"no failed-payment observations in window; provenance={PROVENANCE}; "
                f"env={self.environment}"
            )
        ci = f"[{self.wilson_low:.4f}, {self.wilson_high:.4f}]"
        conf = "low" if self.low_confidence else "measured"
        return (
            f"{conf}: n={self.n_failed_payments}, rate={self.rate_organic}, "
            f"Wilson{ci}; provenance={PROVENANCE}; env={self.environment}"
        )


@dataclass(frozen=True)
class ActionOutcomeRates:
    """All measured (action_type x failure_class) cells for one environment
    and one window, plus provenance + the organic baseline needed to make
    them incremental.
    """

    environment: str
    window_start: datetime
    window_end: datetime
    provenance: str
    min_cell: int
    cells: tuple = field(default_factory=tuple)
    organic: tuple = field(default_factory=tuple)
    incremental: tuple = field(default_factory=tuple)

    def cell_for(self, action_type, failure_class):
        for c in self.cells:
            if c.action_type == action_type and c.failure_class == failure_class:
                return c
        return None

    def organic_for(self, failure_class):
        for o in self.organic:
            if o.failure_class == failure_class:
                return o
        return None

    def incremental_for(self, action_type, failure_class):
        for inc in self.incremental:
            if inc.action_type == action_type and inc.failure_class == failure_class:
                return inc
        return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _window_end(now):
    return now if now is not None else utcnow()


def _window_start(end, days):
    return end - timedelta(days=days)


def _resolve_failure_class(payment, opportunity, cfg):
    """Mirror of RevenueService.opportunity_estimate's class resolution order
    (app/services/revenue/engine.py:255-271). When no payment is attached or
    the payment classifies as UNKNOWN, fall back to the opportunity-type
    default from ``RevenueConfig.opportunity_class_defaults``. When even that
    is absent, return (UNKNOWN, "no_signal") — same as the engine."""
    if payment is not None:
        cls = classify_failure(payment)
        if cls is not FailureClass.UNKNOWN:
            return cls, "payment"
    if opportunity is not None:
        fallback = cfg.opportunity_class_defaults.get(
            opportunity.opportunity_type, FailureClass.UNKNOWN
        )
        if fallback is not FailureClass.UNKNOWN:
            return fallback, "opportunity_type_default"
    return FailureClass.UNKNOWN, "no_signal"


def _env_source_types(environment):
    """Validate the environment + return the commerce ``source_type`` values
    that belong to it. Raises ``ValueError`` for unknown environments — same
    contract as ``source_types_for_environment``."""
    return source_types_for_environment(environment)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def rate_action_outcomes(
    db,
    *,
    environment,
    days=30,
    now=None,
    config=None,
):
    """Per-(action_type x failure_class) measured conversion rates for one
    environment over a trailing window.

    Reads terminal ``recovery_actions`` (``RECOVERED`` + ``FAILED``); UNKNOWN
    is counted and surfaced separately but never folded into the
    denominator (matches engine.py:341-346 semantics). Failure class is
    resolved via the linked payment when present, falling back to the
    opportunity-type default from ``RevenueConfig.opportunity_class_defaults``
    when the payment is missing or classifies as UNKNOWN.

    Each cell carries ``low_confidence=True`` when the verified-terminal
    denominator (``n_recovered + n_failed``) < MIN_CELL — the canonical "do
    not trust the point" marker for the rest of the revenue engine.
    Provenance label is ``measured_from_action_outcomes``.
    """
    end = _window_end(now)
    start = _window_start(end, days)
    cfg = config if config is not None else DEFAULT_CONFIG

    stmt = (
        sa.select(RecoveryOutcomeObservation)
        .where(
            RecoveryOutcomeObservation.environment == environment,
            RecoveryOutcomeObservation.observed_at >= start,
            RecoveryOutcomeObservation.observed_at < end,
            RecoveryOutcomeObservation.observed_status.in_(
                (RecoveryStatus.RECOVERED, RecoveryStatus.FAILED, RecoveryStatus.UNKNOWN)
            ),
        )
        .order_by(RecoveryOutcomeObservation.observed_at, RecoveryOutcomeObservation.id)
    )
    observations = list(db.scalars(stmt))

    opportunity_ids = {o.opportunity_id for o in observations}
    opportunities_by_id = {}
    if opportunity_ids:
        opportunities_by_id = {
            o.id: o
            for o in db.scalars(
                sa.select(RecoveryOpportunity).where(
                    RecoveryOpportunity.id.in_(opportunity_ids)
                )
            )
        }

    payment_ids = {o.payment_id for o in opportunities_by_id.values() if o.payment_id}
    payments_by_id = {}
    if payment_ids:
        payments_by_id = {
            p.id: p
            for p in db.scalars(sa.select(Payment).where(Payment.id.in_(payment_ids)))
        }

    counts = defaultdict(lambda: {"executed": 0, "recovered": 0, "failed": 0, "unknown": 0})
    class_source = {}

    for observation in observations:
        opportunity = opportunities_by_id.get(observation.opportunity_id)
        payment = (
            payments_by_id.get(opportunity.payment_id)
            if opportunity is not None and opportunity.payment_id
            else None
        )
        cls, source = _resolve_failure_class(payment, opportunity, cfg)
        key = (observation.action_type, cls)
        counts[key]["executed"] += 1
        if observation.observed_status is RecoveryStatus.RECOVERED:
            counts[key]["recovered"] += 1
        elif observation.observed_status is RecoveryStatus.FAILED:
            counts[key]["failed"] += 1
        elif observation.observed_status is RecoveryStatus.UNKNOWN:
            counts[key]["unknown"] += 1
        class_source.setdefault(key, source)

    cells = []
    for (action_type, cls), c in sorted(counts.items()):
        n_executed = c["executed"]
        n_recovered = c["recovered"]
        n_failed = c["failed"]
        # Wilson denominator = RECOVERED + FAILED (the verified terminal
        # pair). UNKNOWN is reported but excluded so it cannot deflate the
        # rate.
        denom = n_recovered + n_failed
        rate_point = (n_recovered / denom) if denom > 0 else None
        low, high = wilson_interval(n_recovered, denom, z=WILSON_Z)
        low_conf = denom < MIN_CELL
        cells.append(
            ActionOutcomeCell(
                environment=environment,
                action_type=action_type,
                failure_class=cls,
                failure_class_source=class_source.get((action_type, cls), "no_signal"),
                n_executed=n_executed,
                n_recovered=n_recovered,
                n_failed=n_failed,
                n_unknown=c["unknown"],
                rate_recovered=rate_point,
                wilson_low=low,
                wilson_high=high,
                low_confidence=low_conf,
                sample_confidence=rate_confidence(denom, FULL_CONFIDENCE_SAMPLE),
            )
        )

    return ActionOutcomeRates(
        environment=environment,
        window_start=start,
        window_end=end,
        provenance=PROVENANCE,
        min_cell=MIN_CELL,
        cells=tuple(cells),
    )


def compute_organic_rates(
    db,
    *,
    environment,
    days=30,
    now=None,
    config=None,
):
    """Per-class organic baseline: failed payments that ultimately captured
    *without* a linked ``RECOVERED`` recovery action.

    A payment is "self-captured" when its terminal status is captured and
    there is no ``recovery_actions`` row in the same window with
    ``status == RECOVERED`` linked via the payment's opportunity. This is the
    same definitional stance as the harness's ``self_resolution`` /
    ``organic_return`` columns (app/services/evaluation/outcomes.py), but
    computed from the real executor path on the main DB rather than from
    simulator chains.

    Returns an :class:`ActionOutcomeRates` envelope whose ``cells`` are
    :class:`OrganicRateCell` instances. ``incremental`` is filled in by
    :func:`combine_incremental` once an action-rate report exists for the
    same environment/window.
    """
    end = _window_end(now)
    start = _window_start(end, days)
    cfg = config if config is not None else DEFAULT_CONFIG
    env_source_types = _env_source_types(environment)

    failed_stmt = (
        sa.select(Payment)
        .where(
            Payment.source_type.in_(env_source_types),
            Payment.created_at >= start,
            Payment.created_at < end,
            Payment.status == "failed",
        )
        .order_by(Payment.created_at)
    )
    failed_payments = list(db.scalars(failed_stmt))
    if not failed_payments:
        return ActionOutcomeRates(
            environment=environment,
            window_start=start,
            window_end=end,
            provenance=PROVENANCE,
            min_cell=MIN_CELL,
            cells=tuple(),
        )

    failed_ids = {p.id for p in failed_payments}

    # ``captured`` is the durable boolean the rest of the engine treats as
    # truth; ``status`` may transiently lag.
    captured_ids = {p.id for p in failed_payments if p.captured}

    # Payment ids with any linked opportunity that has a RECOVERED action in
    # the same window — those are *attributed* recoveries, NOT organic.
    attributed_ids = set()
    if failed_ids:
        ts = sa.func.coalesce(RecoveryAction.verified_at, RecoveryAction.completed_at)
        attributed_rows = db.execute(
            sa.select(RecoveryOpportunity.payment_id)
            .join(
                RecoveryAction,
                RecoveryAction.opportunity_id == RecoveryOpportunity.id,
            )
            .where(
                RecoveryOpportunity.payment_id.in_(failed_ids),
                RecoveryAction.environment == environment,
                RecoveryAction.status == RecoveryStatus.RECOVERED,
                ts >= start,
                ts < end,
            )
        ).all()
        attributed_ids = {row[0] for row in attributed_rows if row[0]}

    organic_payments = [p for p in failed_payments if p.id in (captured_ids - attributed_ids)]

    cls_counts = defaultdict(lambda: {"failed": 0, "self_captured": 0})
    for p in failed_payments:
        cls, _ = _resolve_failure_class(p, None, cfg)
        cls_counts[cls]["failed"] += 1
    for p in organic_payments:
        cls, _ = _resolve_failure_class(p, None, cfg)
        cls_counts[cls]["self_captured"] += 1

    organic_cells = []
    for cls, c in sorted(cls_counts.items()):
        n = c["failed"]
        k = c["self_captured"]
        rate = (k / n) if n > 0 else None
        low, high = wilson_interval(k, n, z=WILSON_Z)
        organic_cells.append(
            OrganicRateCell(
                environment=environment,
                failure_class=cls,
                n_failed_payments=n,
                n_self_captured=k,
                rate_organic=rate,
                wilson_low=low,
                wilson_high=high,
                low_confidence=n < MIN_CELL,
                sample_confidence=rate_confidence(n, FULL_CONFIDENCE_SAMPLE),
            )
        )

    return ActionOutcomeRates(
        environment=environment,
        window_start=start,
        window_end=end,
        provenance=PROVENANCE,
        min_cell=MIN_CELL,
        organic=tuple(organic_cells),
    )


def combine_incremental(action):
    """Compute the Newcombe incremental lift cells for an action-rate
    report, paired with the organic baseline cells in the same envelope
    (populated by :func:`compute_organic_rates`). Returns a new envelope
    with ``incremental`` filled in; ``cells`` and ``organic`` are preserved.

    A cell is ``inconclusive`` when its Newcombe CI brackets zero. Consumers
    must keep the prior in that case rather than let the noise move the
    ranking.
    """
    # Import lazily: evaluation.holdout imports the evaluation runner, whose
    # webhook composition root imports this recovery module.
    from app.services.evaluation.holdout import newcombe_ci

    organic_by_cls = {cell.failure_class: cell for cell in action.organic}
    incremental = []
    for cell in action.cells:
        if not isinstance(cell, ActionOutcomeCell):
            continue
        org = organic_by_cls.get(cell.failure_class)
        action_rate = cell.rate_recovered
        organic_rate = org.rate_organic if org is not None else None
        treat_denom = cell.n_recovered + cell.n_failed

        if (
            action_rate is None
            or organic_rate is None
            or treat_denom == 0
            or (org is not None and org.n_failed_payments == 0)
        ):
            incremental.append(
                IncrementalRate(
                    action_type=cell.action_type,
                    failure_class=cell.failure_class,
                    action_rate=action_rate,
                    organic_rate=organic_rate,
                    incremental=None,
                    clamped=0.0,
                    ci_low=0.0,
                    ci_high=0.0,
                    inconclusive=True,
                )
            )
            continue

        treat_ok = cell.n_recovered
        treat_n = treat_denom
        hold_ok = org.n_self_captured
        hold_n = org.n_failed_payments
        ci_low, ci_high = newcombe_ci(treat_ok, treat_n, hold_ok, hold_n)
        diff = action_rate - organic_rate
        inconclusive = ci_low <= 0.0 <= ci_high
        incremental.append(
            IncrementalRate(
                action_type=cell.action_type,
                failure_class=cell.failure_class,
                action_rate=action_rate,
                organic_rate=organic_rate,
                incremental=diff,
                clamped=max(diff, 0.0),
                ci_low=ci_low,
                ci_high=ci_high,
                inconclusive=inconclusive,
            )
        )

    return ActionOutcomeRates(
        environment=action.environment,
        window_start=action.window_start,
        window_end=action.window_end,
        provenance=action.provenance,
        min_cell=action.min_cell,
        cells=action.cells,
        organic=action.organic,
        incremental=tuple(incremental),
    )


__all__ = [
    "ActionOutcomeCell",
    "ActionOutcomeRates",
    "FULL_CONFIDENCE_SAMPLE",
    "IncrementalRate",
    "MIN_CELL",
    "OrganicRateCell",
    "PROVENANCE",
    "WILSON_Z",
    "combine_incremental",
    "compute_organic_rates",
    "rate_action_outcomes",
    "record_outcome_observation",
]

"""Revenue-at-risk engine — the counterfactual core.

Methodology in one paragraph (full write-up in docs/revenue-methodology.md):
failed transactions are NOT lost revenue — many would have failed anyway
(baseline failure rate) and some failures still convert later. So we measure
a per-segment baseline success rate on a pre-incident window, compute the
counterfactual expected revenue for the incident window
(`attempted x baseline_success_rate x avg_order_value`), and call
`observed_loss = counterfactual - actually captured`. Only the failure-class
share of that loss weighted by documented recoverability factors is
`recoverable`, and strategy-level `expected_recovery` discounts that further
by effectiveness priors. `actual_recovered` is not an estimate at all: it is
summed from webhook-verified recovery_actions only.

The service is read-only against the database; it never mutates incidents,
opportunities, or actions.
"""

import math
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.models import (
    ENVIRONMENT_RESEARCH,
    Incident,
    Payment,
    RecoveryAction,
    RecoveryOpportunity,
    source_types_for_environment,
)
from app.ports import ActionType, RecoveryStatus
from app.services.revenue.classify import FailureClass, classify_failure
from app.services.revenue.config import DEFAULT_CONFIG, RevenueConfig
from app.services.revenue.statistics import rate_confidence, wilson_interval
from app.services.revenue.types import (
    Estimate,
    FailureClassBreakdown,
    OpportunityEstimate,
    RecoveredRevenueReport,
    RevenueAtRiskReport,
    SegmentBreakdown,
)

logger = get_logger(__name__)

_CAPTURED_STATUSES = ("captured", "refunded")  # refunded was captured first
_FAILED_STATUS = "failed"
# Every payment status that is neither resolved-success nor resolved-failure
# (created / authorized / ...) is "pending" and excluded from rates AND from
# the loss volume — in-flight payments must not inflate a loss estimate.


def _outcome(payment: Payment) -> str:
    """Resolved outcome of a payment: 'captured' | 'failed' | 'pending'."""
    if payment.captured or payment.status in _CAPTURED_STATUSES:
        return "captured"
    if payment.status == _FAILED_STATUS:
        return "failed"
    return "pending"


def _amount_band(amount_paise: int, edges: tuple[int, ...]) -> str:
    prev: int | None = None
    for edge in edges:
        if amount_paise <= edge:
            return f"le_{edge}" if prev is None else f"{prev}_{edge}"
        prev = edge
    return f"gt_{edges[-1]}" if edges else "all"


def _round_point(value: float) -> int:
    return int(round(value))


def _combine(estimates: Iterable[Estimate], weights: Iterable[float]) -> Estimate:
    """Sum independent-ish estimates into one.

    Band endpoints are summed (a conservative choice: it assumes worst-case
    correlation, so the aggregate band is wide rather than falsely tight).
    The point is the sum of available points; if every component lacks a
    point, the aggregate has none. Confidence is the weight-weighted mean.
    """
    ests = list(estimates)
    ws = list(weights)
    if not ests:
        return Estimate.zero("no contributing segments")
    lower = sum(e.lower_paise for e in ests)
    upper = sum(e.upper_paise for e in ests)
    missing = sum(1 for e in ests if e.point_paise is None)
    known = [e.point_paise for e in ests if e.point_paise is not None]
    point = None if not known else sum(known)  # type: ignore[arg-type]
    total_w = sum(ws)
    confidence = (
        sum(e.confidence * w for e, w in zip(ests, ws)) / total_w if total_w > 0 else 0.0
    )
    low = any(e.low_confidence for e in ests) or confidence < 0.5
    basis = f"sum of {len(ests)} components"
    if missing:
        basis += (
            f"; {missing} component(s) had zero baseline signal -- "
            "their band is included but no point"
        )
    return Estimate(
        point_paise=point,
        lower_paise=lower,
        upper_paise=upper,
        confidence=round(confidence, 4),
        low_confidence=low,
        basis=basis,
    )


class RevenueService:
    """Read-only revenue-at-risk analytics over the shared models."""

    def __init__(self, session: Session, config: RevenueConfig = DEFAULT_CONFIG) -> None:
        self._session = session
        self._cfg = config

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def revenue_at_risk(self, incident_id: str) -> RevenueAtRiskReport:
        """Counterfactual loss analysis for an incident, by segment and by
        failure class. Raises ValueError for an unknown incident id."""
        incident = self._session.get(Incident, incident_id)
        if incident is None:
            raise ValueError(f"incident not found: {incident_id!r}")

        win_end = incident.window_end or incident.detected_at
        win_start = incident.window_start or (win_end - self._cfg.default_incident_window)
        base_end = win_start
        base_start = base_end - self._cfg.baseline_window

        # Environment boundary: baseline/window populations come ONLY from the
        # incident's own environment (commerce rows derive it from source_type).
        source_types = source_types_for_environment(
            incident.environment or ENVIRONMENT_RESEARCH
        )
        baseline_payments = self._payments_between(base_start, base_end, source_types)
        window_payments = self._payments_between(win_start, win_end, source_types)
        returning = self._returning_customer_ids(base_start, source_types)

        baseline_stats = self._segment_stats(baseline_payments, returning)
        window_groups: dict[tuple[str, str, str], list[Payment]] = defaultdict(list)
        for p in window_payments:
            if _outcome(p) == "pending":
                continue
            window_groups[self._segment_key(p, returning)].append(p)

        segments: list[SegmentBreakdown] = []
        # failure-class accumulators: class -> {count, amount, allocated[], recoverable[]}
        fc_acc: dict[FailureClass, dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "amount": 0, "allocated": [], "recoverable": [], "weights": []}
        )

        for key in sorted(window_groups):
            payments = window_groups[key]
            segment, fc_rows = self._analyze_segment(key, payments, baseline_stats)
            segments.append(segment)
            for cls, count, amount, allocated, recoverable in fc_rows:
                acc = fc_acc[cls]
                acc["count"] += count
                acc["amount"] += amount
                acc["allocated"].append(allocated)
                acc["recoverable"].append(recoverable)
                acc["weights"].append(segment.attempted_amount_paise)

        loss_total = _combine(
            [s.observed_loss for s in segments],
            [s.attempted_amount_paise for s in segments],
        )
        recoverable_parts: list[Estimate] = []
        recoverable_weights: list[float] = []
        fc_breakdowns: list[FailureClassBreakdown] = []
        for cls in sorted(fc_acc, key=lambda c: c.value):
            acc = fc_acc[cls]
            allocated = _combine(acc["allocated"], acc["weights"])
            recoverable = _combine(acc["recoverable"], acc["weights"])
            recoverable_parts.append(recoverable)
            recoverable_weights.append(sum(acc["weights"]))
            fc_breakdowns.append(
                FailureClassBreakdown(
                    failure_class=cls.value,
                    failed_count=acc["count"],
                    failed_amount_paise=acc["amount"],
                    allocated_loss=allocated,
                    recoverability_factor=self._cfg.recoverability[cls],
                    recoverable=recoverable,
                )
            )
        recoverable_total = _combine(recoverable_parts, recoverable_weights)

        expected_by_strategy: dict[str, Estimate] = {}
        for action_type, eff in self._cfg.strategy_effectiveness.items():
            if eff <= 0.0:
                continue
            expected_by_strategy[action_type.value] = recoverable_total.scale(
                eff,
                basis=f"recoverable x strategy effectiveness prior ({eff}) for {action_type.value}",
            )

        actual_recovered, recovered_count = self._actual_recovered(incident_id)

        report = RevenueAtRiskReport(
            incident_id=incident.id,
            currency=incident.currency or "INR",
            window_start=win_start,
            window_end=win_end,
            baseline_start=base_start,
            baseline_end=base_end,
            observed_loss=loss_total,
            recoverable=recoverable_total,
            expected_recovery_by_strategy=expected_by_strategy,
            actual_recovered_paise=actual_recovered,
            recovered_actions_count=recovered_count,
            segments=segments,
            failure_classes=fc_breakdowns,
        )
        logger.info(
            "revenue_at_risk computed",
            extra={
                "incident_id": incident.id,
                "observed_loss_point": loss_total.point_paise,
                "recoverable_point": recoverable_total.point_paise,
                "low_confidence": loss_total.low_confidence,
            },
        )
        return report

    def opportunity_estimate(
        self,
        opportunity: RecoveryOpportunity | str,
        *,
        action_type: ActionType | None = None,
    ) -> OpportunityEstimate:
        """Prior-based planning estimate for one opportunity.

        A single payment is a Bernoulli outcome, so the band is the full
        [0, amount] range and low_confidence is always True — these numbers
        rank strategies, they do not promise revenue.
        """
        if isinstance(opportunity, str):
            loaded = self._session.get(RecoveryOpportunity, opportunity)
            if loaded is None:
                raise ValueError(f"opportunity not found: {opportunity!r}")
            opportunity = loaded

        cls = FailureClass.UNKNOWN
        cls_source = "opportunity_type_default"
        if opportunity.payment_id:
            payment = self._session.get(Payment, opportunity.payment_id)
            if payment is not None:
                cls = classify_failure(payment)
                cls_source = "payment"
        if cls is FailureClass.UNKNOWN:
            # A payment with no classifiable signal (e.g. a stuck checkout's
            # empty error telemetry) falls back to the opportunity-type class
            # default instead of pricing at the unknown floor (docs/recovery.md).
            fallback = self._cfg.opportunity_class_defaults.get(
                opportunity.opportunity_type, FailureClass.UNKNOWN
            )
            if fallback is not FailureClass.UNKNOWN:
                cls = fallback
                cls_source = "opportunity_type_default"

        factor = self._cfg.recoverability[cls]
        amount = opportunity.amount_paise
        recoverable = Estimate(
            point_paise=_round_point(amount * factor),
            lower_paise=0,
            upper_paise=amount,
            confidence=self._cfg.prior_confidence,
            low_confidence=True,
            basis=(
                f"prior-based: amount x recoverability({cls.value})={factor}; "
                "single-payment Bernoulli outcome, band is the full range"
            ),
        )

        candidates: dict[ActionType, float]
        if action_type is not None:
            candidates = {action_type: self._cfg.strategy_effectiveness.get(action_type, 0.0)}
        else:
            candidates = dict(self._cfg.strategy_effectiveness)

        expected: dict[str, Estimate] = {}
        best_type: str | None = None
        best_point = -1
        for at, eff in candidates.items():
            if eff <= 0.0:
                continue
            est = recoverable.scale(
                eff, basis=f"recoverable x effectiveness prior ({eff}) for {at.value}"
            )
            expected[at.value] = est
            if est.point_paise is not None and est.point_paise > best_point:
                best_point = est.point_paise
                best_type = at.value

        return OpportunityEstimate(
            opportunity_id=opportunity.id,
            amount_paise=amount,
            currency=opportunity.currency or "INR",
            failure_class=cls.value,
            failure_class_source=cls_source,
            recoverability_factor=factor,
            recoverable=recoverable,
            expected_recovery_by_strategy=expected,
            recommended_action_type=best_type,
        )

    def recovered_revenue(
        self,
        start: datetime,
        end: datetime,
        *,
        incident_id: str | None = None,
    ) -> RecoveredRevenueReport:
        """Measured recovered revenue in [start, end) — the dashboard number.

        Counts only recovery_actions that reached RECOVERED (webhook-verified).
        Actions in UNKNOWN are counted separately and never included.
        """
        ts = sa.func.coalesce(RecoveryAction.verified_at, RecoveryAction.completed_at)
        base_filter = [ts >= start, ts < end]
        if incident_id is not None:
            base_filter.append(RecoveryAction.incident_id == incident_id)

        recovered_stmt = sa.select(RecoveryAction).where(
            RecoveryAction.status == RecoveryStatus.RECOVERED, *base_filter
        )
        actions = list(self._session.scalars(recovered_stmt))

        unknown_stmt = (
            sa.select(sa.func.count())
            .select_from(RecoveryAction)
            .where(RecoveryAction.status == RecoveryStatus.UNKNOWN, *base_filter)
        )
        unknown_count = int(self._session.scalar(unknown_stmt) or 0)

        by_incident: dict[str, int] = defaultdict(int)
        by_type: dict[str, int] = defaultdict(int)
        total = 0
        for a in actions:
            total += a.amount_paise
            by_incident[a.incident_id or "none"] += a.amount_paise
            by_type[a.action_type.value] += a.amount_paise

        return RecoveredRevenueReport(
            window_start=start,
            window_end=end,
            currency="INR",
            total_recovered_paise=total,
            recovered_actions_count=len(actions),
            unknown_actions_count=unknown_count,
            by_incident=dict(by_incident),
            by_action_type=dict(by_type),
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _payments_between(
        self, start: datetime, end: datetime, source_types: tuple[str, ...]
    ) -> list[Payment]:
        stmt = (
            sa.select(Payment)
            .where(
                Payment.created_at >= start,
                Payment.created_at < end,
                Payment.source_type.in_(source_types),
            )
            .order_by(Payment.id)
        )
        return list(self._session.scalars(stmt))

    def _returning_customer_ids(
        self, before: datetime, source_types: tuple[str, ...]
    ) -> set[str]:
        """Customers with at least one captured payment before the baseline
        window — the 'returning' half of new-vs-returning segmentation."""
        stmt = (
            sa.select(Payment.customer_id)
            .where(
                Payment.customer_id.is_not(None),
                Payment.created_at < before,
                Payment.source_type.in_(source_types),
                sa.or_(
                    Payment.captured.is_(True),
                    Payment.status.in_(_CAPTURED_STATUSES),
                ),
            )
            .distinct()
        )
        return {row for row in self._session.scalars(stmt) if row}

    def _segment_key(self, payment: Payment, returning: set[str]) -> tuple[str, str, str]:
        method = payment.method or "unknown"
        band = _amount_band(payment.amount_paise, self._cfg.amount_band_edges_paise)
        if payment.customer_id is None:
            ctype = "unknown"
        else:
            ctype = "returning" if payment.customer_id in returning else "new"
        return (method, band, ctype)

    def _segment_stats(
        self, payments: list[Payment], returning: set[str]
    ) -> dict[tuple[str, str, str], dict[str, int]]:
        stats: dict[tuple[str, str, str], dict[str, int]] = defaultdict(
            lambda: {"n": 0, "captured": 0, "amount": 0}
        )
        for p in payments:
            if _outcome(p) == "pending":
                continue
            acc = stats[self._segment_key(p, returning)]
            acc["n"] += 1
            acc["captured"] += 1 if _outcome(p) == "captured" else 0
            acc["amount"] += p.amount_paise
        return stats

    def _analyze_segment(
        self,
        key: tuple[str, str, str],
        payments: list[Payment],
        baseline_stats: dict[tuple[str, str, str], dict[str, int]],
    ) -> tuple[SegmentBreakdown, list[tuple[FailureClass, int, int, Estimate, Estimate]]]:
        method, band, ctype = key
        attempted_count = len(payments)
        attempted_amount = sum(p.amount_paise for p in payments)
        captured = [p for p in payments if _outcome(p) == "captured"]
        failed = [p for p in payments if _outcome(p) == "failed"]
        captured_amount = sum(p.amount_paise for p in captured)

        base = baseline_stats.get(key)
        n = base["n"] if base else 0
        if n > 0 and base is not None:
            lo, hi = wilson_interval(base["captured"], n, self._cfg.wilson_z)
            rate: float | None = base["captured"] / n
            aov = base["amount"] / n
            confidence = rate_confidence(n, self._cfg.full_confidence_sample)
            basis = f"baseline n={n}, rate={rate:.4f} (Wilson {lo:.4f}..{hi:.4f})"
        else:
            # Zero baseline signal: rate unknown -> full [0,1]; AOV falls back
            # to the incident window's own mix. No defensible point estimate.
            lo, hi, rate = 0.0, 1.0, None
            aov = attempted_amount / attempted_count if attempted_count else 0.0
            confidence = 0.0
            basis = "no baseline data for segment; band spans the full attempted volume"

        cf_point = None if rate is None else attempted_count * rate * aov
        loss_point = None if cf_point is None else max(0.0, cf_point - captured_amount)
        loss_lower = max(0.0, attempted_count * lo * aov - captured_amount)
        loss_upper = max(0.0, attempted_count * hi * aov - captured_amount)
        low_conf = confidence < 0.5 or n < self._cfg.min_baseline_sample

        observed_loss = Estimate(
            point_paise=None if loss_point is None else _round_point(loss_point),
            lower_paise=int(math.floor(loss_lower)),
            upper_paise=int(math.ceil(loss_upper)),
            confidence=round(confidence, 4),
            low_confidence=low_conf,
            basis=basis,
        )

        # Allocate the segment's loss to failure classes by share of failed
        # amount in the incident window, then weight by recoverability.
        fc_rows: list[tuple[FailureClass, int, int, Estimate, Estimate]] = []
        by_class: dict[FailureClass, list[Payment]] = defaultdict(list)
        for p in failed:
            by_class[classify_failure(p)].append(p)
        total_failed_amount = sum(p.amount_paise for p in failed)
        for cls in sorted(by_class, key=lambda c: c.value):
            cls_payments = by_class[cls]
            cls_amount = sum(p.amount_paise for p in cls_payments)
            share = cls_amount / total_failed_amount if total_failed_amount > 0 else 0.0
            allocated = observed_loss.scale(
                share, basis=f"{share:.1%} of segment failed amount is {cls.value}"
            )
            factor = self._cfg.recoverability[cls]
            recoverable = allocated.scale(
                factor, basis=f"allocated loss x recoverability({cls.value})={factor}"
            )
            fc_rows.append((cls, len(cls_payments), cls_amount, allocated, recoverable))

        segment = SegmentBreakdown(
            segment_key=f"method={method}|band={band}|customer={ctype}",
            method=method,
            amount_band=band,
            customer_type=ctype,
            attempted_count=attempted_count,
            failed_count=len(failed),
            captured_count=len(captured),
            attempted_amount_paise=attempted_amount,
            captured_amount_paise=captured_amount,
            baseline_n=n,
            baseline_success_rate=rate,
            baseline_rate_ci=(round(lo, 6), round(hi, 6)),
            avg_order_value_paise=_round_point(aov),
            counterfactual_expected_paise=None if cf_point is None else _round_point(cf_point),
            observed_loss=observed_loss,
        )
        return segment, fc_rows

    def _actual_recovered(self, incident_id: str) -> tuple[int, int]:
        stmt = sa.select(
            sa.func.coalesce(sa.func.sum(RecoveryAction.amount_paise), 0),
            sa.func.count(),
        ).where(
            RecoveryAction.incident_id == incident_id,
            RecoveryAction.status == RecoveryStatus.RECOVERED,
        )
        row = self._session.execute(stmt).one()
        return int(row[0]), int(row[1])

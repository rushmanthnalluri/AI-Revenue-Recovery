"""Strategy generator: ranked recovery-strategy candidates per opportunity.

Probabilistic side of the loop — it PROPOSES; the deterministic policy gate
decides. Every candidate mirrors `ports.StrategyCandidate` and is persisted as
a `recovery_strategies` row so the plan endpoint, the executor, and the audit
trail all reference the same immutable proposal.

Expected recovery comes from `RevenueService.opportunity_estimate` (recoverable
x strategy effectiveness prior). Confidence blends two documented signals:

    confidence = evidence_strength x action_fit

- evidence_strength: the latest ML diagnosis confidence for the incident when
  one exists; otherwise DIAGNOSIS_FREE_EVIDENCE (0.8) — the payment's own
  error fields are direct evidence, but without a corroborating diagnosis the
  proposal stays below the 0.85 auto-execute floor and takes the approval
  lane. Auto-execution therefore requires a diagnosis-backed proposal.
- action_fit: a documented per-(action, failure-class) prior table below.

Recommendation rule (mirrors the assignment contract): the recommended
strategy is the eligible candidate with the highest expected_recovery_paise;
ties break to lower risk, then to candidate order.
"""

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.models import Customer, Diagnosis, RecoveryOpportunity, RecoveryStrategy
from app.ports import ActionType
from app.services.policy import audit
from app.services.revenue import RevenueService
from app.services.revenue.classify import FailureClass, classify_failure
from app.models import Payment

logger = get_logger(__name__)

# Without a diagnosis row the payment's error telemetry is still direct
# evidence — but capped so confidence stays under the policy auto-execute
# floor (0.85) and a human reviews first.
DIAGNOSIS_FREE_EVIDENCE = 0.80

# Razorpay payment links require amount >= 100 paise (INR 1) — research.md.
MIN_PAYMENT_LINK_PAISE = 100

# Candidate generation order; also the final recommendation tiebreak.
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# action_fit[action][failure_class] — P(this action is the right tool | the
# failure belongs to this class). Deliberately conservative priors; rationale
# follows docs/revenue-methodology.md and the network resubmission guidance
# cited there (hard declines are near-zero recoverable by retries).
_RETRY_FIT = {
    FailureClass.TIMEOUT: 0.98,
    FailureClass.SOFT_DECLINE: 0.90,
    FailureClass.UNKNOWN: 0.60,
    FailureClass.ABANDONMENT: 0.50,
    FailureClass.INSUFFICIENT_FUNDS: 0.45,
    FailureClass.HARD_DECLINE: 0.20,
}
_LINK_FIT = {
    FailureClass.ABANDONMENT: 0.90,
    FailureClass.TIMEOUT: 0.80,
    FailureClass.SOFT_DECLINE: 0.75,
    FailureClass.INSUFFICIENT_FUNDS: 0.55,
    FailureClass.UNKNOWN: 0.50,
    FailureClass.HARD_DECLINE: 0.30,
}
_NOTIFY_FIT = {
    FailureClass.ABANDONMENT: 0.70,
    FailureClass.SOFT_DECLINE: 0.50,
    FailureClass.TIMEOUT: 0.45,
    FailureClass.INSUFFICIENT_FUNDS: 0.40,
    FailureClass.UNKNOWN: 0.35,
    FailureClass.HARD_DECLINE: 0.25,
}

# Delayed retry = retry_payment + constraints.delay_seconds. Waiting helps
# when the money is not there right now (payday effect) and costs conversion
# on purely transient failures.
DELAY_SECONDS = 1800
_DELAY_BONUS_INSUFFICIENT_FUNDS = 0.15
_DELAY_PENALTY_DEFAULT = 0.08


@dataclass(frozen=True)
class _Candidate:
    action_type: ActionType
    fit: float
    risk: str
    eligibility: bool
    reason: str
    constraints: dict[str, Any]


class StrategyGenerator:
    """Produces (idempotently) the candidate set for one opportunity."""

    def __init__(self, session: Session, *, revenue: RevenueService | None = None) -> None:
        self._db = session
        self._revenue = revenue or RevenueService(session)

    def generate(
        self,
        opportunity: RecoveryOpportunity | str,
        *,
        generated_by: str = "heuristic",
    ) -> list[RecoveryStrategy]:
        """Find-or-create the strategy set. Re-generation is a no-op: the
        persisted candidates are the proposal of record that policy decisions
        and actions reference."""
        if isinstance(opportunity, str):
            loaded = self._db.get(RecoveryOpportunity, opportunity)
            if loaded is None:
                raise ValueError(f"opportunity not found: {opportunity!r}")
            opportunity = loaded

        existing = self.strategies_for(opportunity.id)
        if existing:
            return existing

        failure_class = self._failure_class(opportunity)
        evidence = self._evidence_strength(opportunity)
        customer = (
            self._db.get(Customer, opportunity.customer_id)
            if opportunity.customer_id
            else None
        )
        estimate = self._revenue.opportunity_estimate(opportunity)

        candidates = self._candidates(opportunity, failure_class, customer)

        def _expected_of(cand: _Candidate) -> int:
            return self._expected_paise(estimate, cand.action_type)

        # Rank: eligible first, then expected recovery desc, risk asc, order.
        ordered = sorted(
            enumerate(candidates),
            key=lambda item: (
                not item[1].eligibility,
                -_expected_of(item[1]),
                _RISK_ORDER.get(item[1].risk, 99),
                item[0],
            ),
        )
        recommended_idx = next(
            (i for i, c in ordered if c.eligibility), None
        )

        rows: list[RecoveryStrategy] = []
        for rank, (orig_idx, cand) in enumerate(ordered):
            row = RecoveryStrategy(
                opportunity_id=opportunity.id,
                action_type=cand.action_type,
                rank=rank,
                expected_recovery_paise=_expected_of(cand),
                confidence=round(evidence * cand.fit, 4),
                risk=cand.risk,
                eligibility=cand.eligibility,
                reason=cand.reason,
                constraints=dict(cand.constraints),
                generated_by=generated_by,
                selected=(orig_idx == recommended_idx),
            )
            self._db.add(row)
            rows.append(row)
        self._db.flush()

        # Backfill the opportunity's planning summary from the recommendation.
        best = next((r for r in rows if r.selected), None)
        if best is not None:
            opportunity.expected_recovery_paise = best.expected_recovery_paise
            opportunity.confidence = best.confidence
            opportunity.risk = best.risk

        audit.record(
            self._db,
            actor=(
                generated_by
                if generated_by.startswith(("agent:", "human:", "system:"))
                else f"agent:strategy_generator:{generated_by}"
            ),
            action="recovery.strategies_generated",
            entity_type="recovery_opportunity",
            entity_id=opportunity.id,
            details={
                "failure_class": failure_class.value,
                "evidence_strength": evidence,
                "candidates": [
                    {
                        "strategy_id": r.id,
                        "action_type": r.action_type.value,
                        "expected_recovery_paise": r.expected_recovery_paise,
                        "confidence": r.confidence,
                        "risk": r.risk,
                        "eligibility": r.eligibility,
                        "selected": r.selected,
                    }
                    for r in rows
                ],
            },
        )
        logger.info(
            "recovery strategies generated",
            extra={
                "opportunity_id": opportunity.id,
                "failure_class": failure_class.value,
                "recommended": best.action_type.value if best else None,
            },
        )
        return rows

    def strategies_for(self, opportunity_id: str) -> list[RecoveryStrategy]:
        stmt = (
            sa.select(RecoveryStrategy)
            .where(RecoveryStrategy.opportunity_id == opportunity_id)
            .order_by(RecoveryStrategy.rank, RecoveryStrategy.id)
        )
        return list(self._db.scalars(stmt))

    def recommended_for(self, opportunity_id: str) -> RecoveryStrategy | None:
        rows = self.strategies_for(opportunity_id)
        return next((r for r in rows if r.selected), None)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _failure_class(self, opportunity: RecoveryOpportunity) -> FailureClass:
        if opportunity.payment_id:
            payment = self._db.get(Payment, opportunity.payment_id)
            if payment is not None:
                return classify_failure(payment)
        # No classifiable payment: use the estimate's documented default.
        est = self._revenue.opportunity_estimate(opportunity)
        try:
            return FailureClass(est.failure_class)
        except ValueError:
            return FailureClass.UNKNOWN

    def _evidence_strength(self, opportunity: RecoveryOpportunity) -> float:
        if opportunity.incident_id:
            stmt = (
                sa.select(Diagnosis)
                .where(Diagnosis.incident_id == opportunity.incident_id)
                .order_by(Diagnosis.version.desc(), Diagnosis.id.desc())
                .limit(1)
            )
            diagnosis = self._db.scalar(stmt)
            if diagnosis is not None:
                return max(0.0, min(1.0, float(diagnosis.confidence)))
        return DIAGNOSIS_FREE_EVIDENCE

    @staticmethod
    def _expected_paise(estimate, action_type: ActionType) -> int:
        est = estimate.expected_recovery_by_strategy.get(action_type.value)
        if est is None or est.point_paise is None:
            return 0
        return int(est.point_paise)

    def _candidates(
        self,
        opportunity: RecoveryOpportunity,
        failure_class: FailureClass,
        customer: Customer | None,
    ) -> list[_Candidate]:
        amount = opportunity.amount_paise
        has_payment = opportunity.payment_id is not None
        hard = failure_class is FailureClass.HARD_DECLINE
        opted_out = bool(customer.opted_out) if customer else False
        cls = failure_class.value

        retry_fit = _RETRY_FIT[failure_class]
        if failure_class is FailureClass.INSUFFICIENT_FUNDS:
            delay_fit = min(0.99, retry_fit + _DELAY_BONUS_INSUFFICIENT_FUNDS)
            delay_reason = (
                "delayed retry gives the customer time to replenish funds "
                "(payday effect); immediate retry would likely decline again"
            )
        else:
            delay_fit = max(0.0, retry_fit - _DELAY_PENALTY_DEFAULT)
            delay_reason = (
                f"delayed retry after {DELAY_SECONDS}s in case the {cls} condition "
                "clears; costs some conversion versus an immediate retry"
            )

        retry_eligible = has_payment and not hard
        return [
            _Candidate(
                ActionType.RETRY_PAYMENT,
                retry_fit,
                "medium",
                retry_eligible,
                (
                    f"retry the failed payment via a fresh idempotency-keyed order; "
                    f"failure class {cls} is "
                    + ("transient and usually recoverable" if retry_fit >= 0.8 else
                       "only sometimes recoverable" if retry_fit >= 0.4 else
                       "rarely recoverable by resubmission")
                ) if retry_eligible else (
                    "retry requires a linked failed payment"
                    if not has_payment
                    else "network rules discourage resubmitting never-approve (hard) declines"
                ),
                {},
            ),
            _Candidate(
                ActionType.RETRY_PAYMENT,
                delay_fit,
                "medium",
                retry_eligible,
                delay_reason if retry_eligible else "delayed retry requires a linked failed payment",
                {"delay_seconds": DELAY_SECONDS},
            ),
            _Candidate(
                ActionType.CREATE_PAYMENT_LINK,
                _LINK_FIT[failure_class],
                "low",
                amount >= MIN_PAYMENT_LINK_PAISE,
                (
                    f"send a fresh payment link (reference_id = idempotency key); "
                    f"strongest tool for {cls} when the customer must re-attempt"
                ) if amount >= MIN_PAYMENT_LINK_PAISE else (
                    f"amount {amount} paise is below the payment-link minimum"
                ),
                {},
            ),
            _Candidate(
                ActionType.NOTIFY_CUSTOMER,
                _NOTIFY_FIT[failure_class],
                "low",
                customer is not None and not opted_out,
                (
                    f"nudge the customer about the incomplete payment ({cls}); "
                    "no money moves, the customer completes the payment themselves"
                ) if customer is not None and not opted_out else (
                    "customer has opted out of automated recovery contact"
                    if opted_out
                    else "no customer attached to this opportunity"
                ),
                {"channel": "notification"},
            ),
            _Candidate(
                ActionType.ESCALATE_HUMAN,
                1.0,
                "low",
                True,
                "hand to a human operator; always eligible, never auto-executes revenue",
                {"queue": "human_ops"},
            ),
            _Candidate(
                ActionType.NO_ACTION,
                1.0,
                "low",
                True,
                "baseline: do nothing and let organic recovery happen",
                {},
            ),
        ]


__all__ = [
    "DELAY_SECONDS",
    "DIAGNOSIS_FREE_EVIDENCE",
    "StrategyGenerator",
]

"""Opportunity builder: turn an incident's blast radius into recovery
opportunities — one row per recoverable unit of work.

PER-PAYMENT, NOT BATCHED — deliberate design choice:
- Verification links recovery actions to gateway truth via
  `recovery_opportunities.payment_id` (the webhook reconciler in
  app.api.v1.webhooks resolves actions through exactly this column). A batched
  opportunity could never prove WHICH payment was recovered.
- The policy gate's stateful guards (per-customer rate limits, duplicate
  protection) key off the opportunity's customer — per-payment rows keep those
  guards precise.
- Idempotency is per (incident, payment) / (incident, order), so re-running
  the builder after new webhook arrivals only adds the delta.

Idempotence: re-running `build_for_incident` never creates duplicates. A
payment already linked to an opportunity of this incident is skipped, as is an
order already represented by a `dropped_checkout` opportunity (tracked via
`meta["order_id"]` — no schema change needed) and a subscription already
represented by a `subscription_halted` opportunity (tracked via the real
`subscription_id` FK column).

DEDUP RULE (one opportunity per incident+checkout): a checkout is counted
exactly once, however many sources could describe it.
- Payment-level wins at selection time: an order with ANY payment row (failed
  or stuck-created) is excluded from the order-level `dropped_checkout` path —
  the payment's own opportunity already covers the checkout.
- First-write wins across builds: a stuck payment whose order is already
  represented in this incident (by an order-level `dropped_checkout` from an
  earlier build, or by a sibling stuck attempt on the same order) is skipped —
  the existing opportunity already carries the checkout's customer and amount,
  and its payment-link recovery is identical. The skip is reported in
  `BuildResult.existing`, never silently dropped.
Two DISTINCT payments on one order (e.g. a failed attempt plus a later stuck
one) are NOT merged: they are separate attempts, and the policy gate's
duplicate protection already prevents double-firing the customer.
"""

from dataclasses import dataclass, field
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.logging import get_logger
from app.models import Incident, Order, Payment, RecoveryOpportunity, Subscription
from app.models.base import ENVIRONMENT_RESEARCH, source_types_for_environment
from app.ports import RecoveryStatus
from app.services.policy import audit

logger = get_logger(__name__)

# How long a fresh opportunity stays actionable. 72h covers the window in
# which retries/links still convert; after that the payment is stale.
OPPORTUNITY_TTL = timedelta(hours=72)

# Mirrors RevenueService._cfg.default_incident_window for incidents whose
# window has not been backfilled by detection yet.
_DEFAULT_WINDOW = timedelta(hours=1)

_FAILED_STATUS = "failed"
_ORDER_OPEN_STATUS = "created"  # Razorpay order created but never paid
_STUCK_STATUS = "created"  # payment created but never resolved to a terminal state

# A payment still in `created` this long after creation is stuck: the customer
# never completed the checkout. Mirrors the detection engine's
# checkout_abandonment_rate inactivity threshold
# (app/services/detection/series.py, inactivity_minutes=30), but evaluated
# against the build's knowledge edge (now) rather than the detection pass's
# window end — the builder acts in the present, so a payment created late in
# the window still qualifies once it is genuinely stuck. Payments younger than
# the threshold may still be in flight and are honestly excluded.
STUCK_CREATED_THRESHOLD = timedelta(minutes=30)

# Opportunity type for payments stuck in `created` (checkout abandonment at
# the payment level — the order-level equivalent is `dropped_checkout`).
STUCK_CHECKOUT_PAYMENT_TYPE = "stuck_checkout_payment"

# Opportunity type for Razorpay subscriptions stuck in `pending`/`halted`:
# Razorpay's own dunning retries have stopped there, so the outstanding
# (arrears) amount is PulseRecover's lane — recovered via a fresh payment
# link, never a blind retry.
SUBSCRIPTION_HALTED_TYPE = "subscription_halted"

# Subscription states in which the gateway no longer retries the charge.
_SUBSCRIPTION_STUCK_STATUSES = ("pending", "halted")


@dataclass(frozen=True)
class BuildResult:
    incident_id: str
    created: list[RecoveryOpportunity] = field(default_factory=list)
    existing: list[RecoveryOpportunity] = field(default_factory=list)

    @property
    def all(self) -> list[RecoveryOpportunity]:
        return [*self.created, *self.existing]


class OpportunityBuilder:
    """Identifies recoverable work inside an incident window. Read-only on
    incidents/payments/orders; writes only recovery_opportunities (+audit)."""

    def __init__(self, session: Session) -> None:
        self._db = session

    def build_for_incident(
        self, incident_id: str, *, actor: str = "system:builder"
    ) -> BuildResult:
        incident = self._db.get(Incident, incident_id)
        if incident is None:
            raise ValueError(f"incident not found: {incident_id!r}")

        win_end = incident.window_end or incident.detected_at
        win_start = incident.window_start or (win_end - _DEFAULT_WINDOW)
        # Environment boundary: candidate payments/orders come ONLY from the
        # incident's own environment, and every opportunity inherits it.
        source_types = source_types_for_environment(
            incident.environment or ENVIRONMENT_RESEARCH
        )

        already = self._existing_index(incident.id)
        result = BuildResult(incident_id=incident.id)
        knowledge_edge = utcnow()

        for payment in self._failed_payments(win_start, win_end, source_types):
            if payment.id in already["payments"]:
                result.existing.append(already["payments"][payment.id])
                continue
            opp = self._new_opportunity(
                incident,
                opportunity_type="failed_payment_retry",
                payment_id=payment.id,
                customer_id=payment.customer_id,
                amount_paise=payment.amount_paise,
                currency=payment.currency,
                reason=(
                    f"payment {payment.gateway_payment_id or payment.id} failed "
                    f"({payment.error_description or payment.error_code or 'no error detail'}) "
                    f"inside the incident window"
                ),
                meta={"gateway_payment_id": payment.gateway_payment_id},
                actor=actor,
            )
            result.created.append(opp)
            already["payments"][payment.id] = opp

        for payment in self._stuck_created_payments(win_start, win_end, knowledge_edge, source_types):
            if payment.id in already["payments"]:
                result.existing.append(already["payments"][payment.id])
                continue
            if payment.order_id and payment.order_id in already["orders"]:
                # First-write wins: the checkout is already represented (an
                # order-level dropped_checkout from an earlier build, or a
                # sibling stuck attempt seen above) — never double-count it.
                result.existing.append(already["orders"][payment.order_id])
                continue
            opp = self._new_opportunity(
                incident,
                opportunity_type=STUCK_CHECKOUT_PAYMENT_TYPE,
                payment_id=payment.id,
                customer_id=payment.customer_id,
                amount_paise=payment.amount_paise,
                currency=payment.currency,
                reason=(
                    f"payment {payment.gateway_payment_id or payment.id} has been stuck "
                    f"in 'created' for over "
                    f"{int(STUCK_CREATED_THRESHOLD.total_seconds() // 60)} minutes — "
                    "the customer never completed the checkout inside the "
                    "incident window"
                ),
                meta={
                    "gateway_payment_id": payment.gateway_payment_id,
                    "order_id": payment.order_id,
                },
                actor=actor,
            )
            result.created.append(opp)
            already["payments"][payment.id] = opp
            if payment.order_id:
                already["orders"][payment.order_id] = opp

        for order in self._abandoned_orders(win_start, win_end, source_types):
            if order.id in already["orders"]:
                result.existing.append(already["orders"][order.id])
                continue
            opp = self._new_opportunity(
                incident,
                opportunity_type="dropped_checkout",
                payment_id=None,
                customer_id=order.customer_id,
                amount_paise=order.amount_paise,
                currency=order.currency,
                reason=(
                    f"order {order.gateway_order_id or order.id} was created but no "
                    "payment was ever attempted inside the incident window"
                ),
                meta={"order_id": order.id, "gateway_order_id": order.gateway_order_id},
                actor=actor,
            )
            result.created.append(opp)
            already["orders"][order.id] = opp

        for subscription in self._stuck_subscriptions(source_types):
            if subscription.id in already["subscriptions"]:
                result.existing.append(already["subscriptions"][subscription.id])
                continue
            opp = self._new_opportunity(
                incident,
                opportunity_type=SUBSCRIPTION_HALTED_TYPE,
                payment_id=None,
                subscription_id=subscription.id,
                customer_id=subscription.customer_id,
                amount_paise=subscription.amount_paise,
                currency=subscription.currency,
                reason=(
                    f"subscription {subscription.gateway_subscription_id or subscription.id} "
                    f"is stuck in '{subscription.status}' — Razorpay's dunning retries "
                    "have stopped; the outstanding (arrears) amount is recoverable "
                    "via a fresh payment link"
                ),
                meta={
                    "subscription_id": subscription.id,
                    "gateway_subscription_id": subscription.gateway_subscription_id,
                    "subscription_status": subscription.status,
                },
                actor=actor,
            )
            result.created.append(opp)
            already["subscriptions"][subscription.id] = opp

        logger.info(
            "recovery opportunities built",
            extra={
                "incident_id": incident.id,
                "created_count": len(result.created),
                "window_start": win_start.isoformat(),
                "window_end": win_end.isoformat(),
            },
        )
        return result

    # ------------------------------------------------------------------
    # selection queries
    # ------------------------------------------------------------------

    def _failed_payments(self, start, end, source_types) -> list[Payment]:
        stmt = (
            sa.select(Payment)
            .where(
                Payment.status == _FAILED_STATUS,
                Payment.created_at >= start,
                Payment.created_at < end,
                Payment.source_type.in_(source_types),
            )
            .order_by(Payment.created_at, Payment.id)
        )
        return list(self._db.scalars(stmt))

    def _stuck_created_payments(self, start, end, knowledge_edge, source_types) -> list[Payment]:
        """Payments created inside the window that are STILL in `created` and
        have been so for at least STUCK_CREATED_THRESHOLD as of the build's
        knowledge edge. A payment that resolved (captured/failed) is covered
        by the other sources or needs no recovery; one younger than the
        threshold may still be in flight — never flagged as abandoned."""
        stuck_before = knowledge_edge - STUCK_CREATED_THRESHOLD
        stmt = (
            sa.select(Payment)
            .where(
                Payment.status == _STUCK_STATUS,
                Payment.created_at >= start,
                Payment.created_at < end,
                Payment.created_at <= stuck_before,
                Payment.source_type.in_(source_types),
            )
            .order_by(Payment.created_at, Payment.id)
        )
        return list(self._db.scalars(stmt))

    def _abandoned_orders(self, start, end, source_types) -> list[Order]:
        """Orders still in `created` state with NO payment rows at all. An
        order with a failed or stuck-created payment is already covered by
        that payment's own opportunity — counting both would double the
        revenue at risk (payment-level wins; see the module docstring)."""
        has_payment = (
            sa.select(Payment.id).where(Payment.order_id == Order.id).correlate(Order).exists()
        )
        stmt = (
            sa.select(Order)
            .where(
                Order.status == _ORDER_OPEN_STATUS,
                Order.created_at >= start,
                Order.created_at < end,
                Order.source_type.in_(source_types),
                ~has_payment,
            )
            .order_by(Order.created_at, Order.id)
        )
        return list(self._db.scalars(stmt))

    def _stuck_subscriptions(self, source_types) -> list[Subscription]:
        """Subscriptions currently stuck in `pending`/`halted`. Selected by
        CURRENT status rather than the incident window: the stuck state is a
        present-tense signal (there is no per-subscription status-change
        timestamp to window on), and per-incident dedupe keeps re-runs
        idempotent. Environment scoping rides on source_type exactly like the
        commerce-row sources above."""
        stmt = (
            sa.select(Subscription)
            .where(
                Subscription.status.in_(_SUBSCRIPTION_STUCK_STATUSES),
                Subscription.source_type.in_(source_types),
            )
            .order_by(Subscription.created_at, Subscription.id)
        )
        return list(self._db.scalars(stmt))

    # ------------------------------------------------------------------
    # idempotency + writes
    # ------------------------------------------------------------------

    def _incident_opportunities(self, incident_id: str) -> list[RecoveryOpportunity]:
        stmt = (
            sa.select(RecoveryOpportunity)
            .where(RecoveryOpportunity.incident_id == incident_id)
            .order_by(RecoveryOpportunity.created_at, RecoveryOpportunity.id)
        )
        return list(self._db.scalars(stmt))

    def _existing_index(
        self, incident_id: str
    ) -> dict[str, dict[str, RecoveryOpportunity]]:
        payments: dict[str, RecoveryOpportunity] = {}
        orders: dict[str, RecoveryOpportunity] = {}
        subscriptions: dict[str, RecoveryOpportunity] = {}
        for opp in self._incident_opportunities(incident_id):
            if opp.payment_id:
                payments[opp.payment_id] = opp
            order_id = (opp.meta or {}).get("order_id")
            if order_id:
                orders[order_id] = opp
            if opp.subscription_id:
                subscriptions[opp.subscription_id] = opp
        return {"payments": payments, "orders": orders, "subscriptions": subscriptions}

    def _new_opportunity(
        self,
        incident: Incident,
        *,
        opportunity_type: str,
        payment_id: str | None,
        customer_id: str | None,
        amount_paise: int,
        currency: str,
        reason: str,
        meta: dict,
        actor: str,
        subscription_id: str | None = None,
    ) -> RecoveryOpportunity:
        now = utcnow()
        opp = RecoveryOpportunity(
            incident_id=incident.id,
            payment_id=payment_id,
            subscription_id=subscription_id,
            customer_id=customer_id,
            opportunity_type=opportunity_type,
            status=RecoveryStatus.PROPOSED,
            amount_paise=amount_paise,
            currency=currency or "INR",
            reason=reason,
            expires_at=now + OPPORTUNITY_TTL,
            meta={k: v for k, v in meta.items() if v is not None},
            # Opportunities inherit the incident's environment — the executor
            # routes the gateway by exactly this stamp.
            environment=incident.environment or ENVIRONMENT_RESEARCH,
        )
        self._db.add(opp)
        self._db.flush()
        entry = audit.record(
            self._db,
            actor=actor,
            action="recovery.opportunity_created",
            entity_type="recovery_opportunity",
            entity_id=opp.id,
            details={
                "incident_id": incident.id,
                "opportunity_type": opportunity_type,
                "payment_id": payment_id,
                "subscription_id": subscription_id,
                "amount_paise": amount_paise,
            },
        )
        entry.environment = opp.environment
        return opp


__all__ = [
    "BuildResult",
    "OPPORTUNITY_TTL",
    "STUCK_CHECKOUT_PAYMENT_TYPE",
    "STUCK_CREATED_THRESHOLD",
    "SUBSCRIPTION_HALTED_TYPE",
    "OpportunityBuilder",
]

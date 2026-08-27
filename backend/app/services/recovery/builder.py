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
`meta["order_id"]` — no schema change needed).
"""

from dataclasses import dataclass, field
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.logging import get_logger
from app.models import Incident, Order, Payment, RecoveryOpportunity
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

        already = self._existing_index(incident.id)
        result = BuildResult(incident_id=incident.id)

        for payment in self._failed_payments(win_start, win_end):
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

        for order in self._abandoned_orders(win_start, win_end):
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

    def _failed_payments(self, start, end) -> list[Payment]:
        stmt = (
            sa.select(Payment)
            .where(
                Payment.status == _FAILED_STATUS,
                Payment.created_at >= start,
                Payment.created_at < end,
            )
            .order_by(Payment.created_at, Payment.id)
        )
        return list(self._db.scalars(stmt))

    def _abandoned_orders(self, start, end) -> list[Order]:
        """Orders still in `created` state with NO payment rows at all. An
        order with a failed payment is already covered by that payment's
        failed_payment_retry opportunity — counting both would double the
        revenue at risk."""
        has_payment = (
            sa.select(Payment.id).where(Payment.order_id == Order.id).correlate(Order).exists()
        )
        stmt = (
            sa.select(Order)
            .where(
                Order.status == _ORDER_OPEN_STATUS,
                Order.created_at >= start,
                Order.created_at < end,
                ~has_payment,
            )
            .order_by(Order.created_at, Order.id)
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
        for opp in self._incident_opportunities(incident_id):
            if opp.payment_id:
                payments[opp.payment_id] = opp
            order_id = (opp.meta or {}).get("order_id")
            if order_id:
                orders[order_id] = opp
        return {"payments": payments, "orders": orders}

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
    ) -> RecoveryOpportunity:
        now = utcnow()
        opp = RecoveryOpportunity(
            incident_id=incident.id,
            payment_id=payment_id,
            customer_id=customer_id,
            opportunity_type=opportunity_type,
            status=RecoveryStatus.PROPOSED,
            amount_paise=amount_paise,
            currency=currency or "INR",
            reason=reason,
            expires_at=now + OPPORTUNITY_TTL,
            meta={k: v for k, v in meta.items() if v is not None},
        )
        self._db.add(opp)
        self._db.flush()
        audit.record(
            self._db,
            actor=actor,
            action="recovery.opportunity_created",
            entity_type="recovery_opportunity",
            entity_id=opp.id,
            details={
                "incident_id": incident.id,
                "opportunity_type": opportunity_type,
                "payment_id": payment_id,
                "amount_paise": amount_paise,
            },
        )
        return opp


__all__ = ["BuildResult", "OPPORTUNITY_TTL", "OpportunityBuilder"]

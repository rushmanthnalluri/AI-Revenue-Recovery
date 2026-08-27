"""Webhook verification handlers — the recovery loop's verification side.

This registry owns the *verification logic* for inbound Razorpay webhook
events; `app.api.v1.webhooks` is only the ingress adapter (HMAC signature
gate, `x-razorpay-event-id` dedup, raw-event persistence, fast ack) and calls
`dispatch_event` here. The reconciliation sweep (`reconcile.py`) re-runs
failed events through the exact same registry, so a reprocessed event behaves
bit-for-bit like a live one.

Handler contract (`handler(db, payload) -> str | None`):
- Return `None` when the event is fully handled -> the row is marked
  `processed=True`.
- Return a note when the event could not be resolved (e.g. unknown payment)
  -> `processed=False`, reconcilable later.
- Raise and `dispatch_event` rolls back the handler's partial writes, keeps
  the stored event, and marks it `processed=False`.

Handlers must be idempotent and out-of-order safe:
- `payment.failed` is NOT terminal: a later `payment.captured` for the same
  payment wins, so a captured payment never regresses to failed, and a
  FAILED-linked recovery action can still transition to RECOVERED.
- `captured` is terminal for payments: a late `payment.failed` is a no-op.

Transaction boundary: like every service here, handlers flush but NEVER
commit — the API layer (or the reconcile sweep's caller) owns the commit.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.logging import get_logger, request_id_ctx
from app.models import (
    AuditLog,
    Payment,
    PaymentEvent,
    RecoveryAction,
    RecoveryOpportunity,
)
from app.ports import ActionType, RecoveryStatus

logger = get_logger(__name__)

# Action states from which a verification event may still move the action.
_OPEN_ACTION_STATES = (
    RecoveryStatus.EXECUTING,
    RecoveryStatus.VERIFYING,
    RecoveryStatus.FAILED,  # failed is not terminal: late capture may still win
)


def dispatch_event(
    db: Session, event_type: str, payload: dict[str, Any]
) -> tuple[bool, str | None]:
    """Run one stored event through the handler registry.

    Returns `(processed, detail)` — the caller persists them on the
    `webhook_events` row. `processed=False` keeps the event reconcilable.
    """
    handler = EVENT_HANDLERS.get(event_type)
    if handler is None:
        return True, f"event {event_type!r} stored; no handler registered"
    try:
        detail = handler(db, payload)
    except Exception as exc:  # keep the stored event; reconcile later
        db.rollback()
        logger.exception(
            "webhook handler failed", extra={"event_type": event_type}
        )
        return False, f"handler error: {type(exc).__name__}: {exc}"
    return detail is None, detail


# ---------------------------------------------------------------------------
# Handler registry: event_type -> handler(db, payload) -> unresolved note|None
# ---------------------------------------------------------------------------


def _handle_payment_captured(db: Session, payload: dict[str, Any]) -> str | None:
    entity = _entity(payload, "payment")
    if entity is None or not entity.get("id"):
        return "payload missing payment entity"
    payment = _find_payment(db, entity["id"])
    if payment is None:
        return f"unknown payment {entity['id']}; stored for reconciliation"
    occurred_at = _event_ts(payload)
    if payment.status != "captured":
        _transition_payment(db, payment, entity, "captured", occurred_at)
        payment.captured = True
    # Late success wins over an earlier failure for linked recovery actions.
    for action in _linked_actions(db, payment):
        _mark_action(db, action, RecoveryStatus.RECOVERED, "payment.captured")
    return None


def _handle_payment_failed(db: Session, payload: dict[str, Any]) -> str | None:
    entity = _entity(payload, "payment")
    if entity is None or not entity.get("id"):
        return "payload missing payment entity"
    payment = _find_payment(db, entity["id"])
    if payment is None:
        return f"unknown payment {entity['id']}; stored for reconciliation"
    if payment.status == "captured":
        # Captured is terminal for payments; a late failed event is a no-op.
        return None
    occurred_at = _event_ts(payload)
    if payment.status != "failed":
        _transition_payment(db, payment, entity, "failed", occurred_at)
    payment.captured = False
    payment.error_code = entity.get("error_code")
    payment.error_description = entity.get("error_description")
    payment.error_source = entity.get("error_source")
    if entity.get("method"):
        payment.method = entity.get("method")
    reason = entity.get("error_reason") or entity.get("error_description") or "payment.failed"
    for action in _linked_actions(db, payment, terminal_ok=False):
        _mark_action(db, action, RecoveryStatus.FAILED, "payment.failed", error=str(reason))
    return None


def _handle_payment_link_paid(db: Session, payload: dict[str, Any]) -> str | None:
    link = _entity(payload, "payment_link")
    if link is None or not link.get("id"):
        return "payload missing payment_link entity"
    # payment_link.paid may also carry the capturing payment entity.
    payment_entity = _entity(payload, "payment")
    if payment_entity and payment_entity.get("id"):
        payment = _find_payment(db, payment_entity["id"])
        if payment is not None and payment.status != "captured":
            _transition_payment(db, payment, payment_entity, "captured", _event_ts(payload))
            payment.captured = True

    reference_id = link.get("reference_id")
    note: str | None = None
    action: RecoveryAction | None = None
    if reference_id:
        action = db.scalar(
            sa.select(RecoveryAction).where(
                RecoveryAction.gateway_request_id == reference_id,
                RecoveryAction.action_type == ActionType.CREATE_PAYMENT_LINK,
            )
        )
    if action is None:
        note = f"no recovery action linked to reference_id {reference_id!r}"
    elif action.status is RecoveryStatus.RECOVERED:
        pass  # idempotent: already verified
    elif action.status in _OPEN_ACTION_STATES:
        _mark_action(db, action, RecoveryStatus.RECOVERED, "payment_link.paid")
    else:
        note = f"linked action {action.id} in state {action.status}; left unchanged"
    return note


EVENT_HANDLERS: dict[str, Callable[[Session, dict[str, Any]], str | None]] = {
    "payment.captured": _handle_payment_captured,
    "payment.failed": _handle_payment_failed,
    "payment_link.paid": _handle_payment_link_paid,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _entity(payload: dict[str, Any], kind: str) -> dict[str, Any] | None:
    node = payload.get("payload")
    if not isinstance(node, dict):
        return None
    wrapper = node.get(kind)
    if not isinstance(wrapper, dict):
        return None
    entity = wrapper.get("entity")
    return entity if isinstance(entity, dict) else None


def _find_payment(db: Session, gateway_payment_id: str) -> Payment | None:
    return db.scalar(
        sa.select(Payment).where(Payment.gateway_payment_id == gateway_payment_id)
    )


def _event_ts(payload: dict[str, Any]) -> datetime:
    ts = payload.get("created_at")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return utcnow()


def _transition_payment(
    db: Session,
    payment: Payment,
    entity: dict[str, Any],
    to_status: str,
    occurred_at: datetime,
) -> None:
    from_status = payment.status
    payment.status = to_status
    db.add(
        PaymentEvent(
            payment_id=payment.id,
            event_type=f"payment.{to_status}",
            from_status=from_status,
            to_status=to_status,
            source="webhook",
            payload=entity,
            occurred_at=occurred_at,
        )
    )


def _linked_actions(
    db: Session, payment: Payment, *, terminal_ok: bool = True
) -> list[RecoveryAction]:
    """Recovery actions tied to this payment via their opportunity."""
    states = list(_OPEN_ACTION_STATES if terminal_ok else _OPEN_ACTION_STATES[:2])
    return list(
        db.scalars(
            sa.select(RecoveryAction)
            .join(
                RecoveryOpportunity,
                RecoveryAction.opportunity_id == RecoveryOpportunity.id,
            )
            .where(
                RecoveryOpportunity.payment_id == payment.id,
                RecoveryAction.status.in_(states),
            )
        )
    )


def _mark_action(
    db: Session,
    action: RecoveryAction,
    status: RecoveryStatus,
    trigger: str,
    *,
    error: str | None = None,
) -> None:
    now = utcnow()
    from_status = action.status
    action.status = status
    action.last_error = error
    if status is RecoveryStatus.RECOVERED:
        action.verified_at = now
    action.completed_at = now
    # Keep the opportunity's stored status in lockstep with its action
    # (mirrors RecoveryExecutor._sync_opportunity) so status-filtered list
    # queries agree with the projected status the API displays.
    if action.opportunity is not None:
        action.opportunity.status = status
    db.add(
        AuditLog(
            created_at=now,
            entity_type="recovery_action",
            entity_id=action.id,
            actor="system:webhook",
            action=f"verify_{status.value.lower()}",
            details={
                "trigger": trigger,
                "from_status": from_status.value,
                "to_status": status.value,
                "error": error,
            },
            request_id=request_id_ctx.get(),
        )
    )
    logger.info(
        "recovery action verification update",
        extra={
            "action_id": action.id,
            "from_status": from_status.value,
            "to_status": status.value,
            "trigger": trigger,
        },
    )


__all__ = ["EVENT_HANDLERS", "dispatch_event"]

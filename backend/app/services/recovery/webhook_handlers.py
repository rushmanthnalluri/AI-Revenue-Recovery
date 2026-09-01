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

`payment_link.paid` amount verification (financial safety): the `reference_id`
anchor proves WHICH action the link belongs to, not that the paid amount is
the amount we asked for. Before marking RECOVERED the handler cross-checks
the link entity against the action:
- `amount` must be an integer equal to `action.amount_paise` (exact match;
  a missing/non-integer amount is unverifiable and fails closed);
- `currency`, when present in the payload, must equal `action.currency`;
- partial payments NEVER count as recovered: `status == "partial_paid"` or
  an integer `amount_paid` below the link `amount` holds the action (only a
  fully-paid link recovers; when the customer completes a partial link,
  Razorpay fires a fresh `payment_link.paid` with the full `amount_paid`,
  which then verifies and recovers — the hold is not terminal).
Any mismatch holds the action in its current open state (VERIFYING in the
normal flow), records `last_error`, and appends a `verification.amount_mismatch`
audit row with expected vs actual. The event itself is still marked processed
(handled): the payload is immutable, so reprocessing could only duplicate the
hold — a LATER event carrying corrected amounts recovers the held action.

Ack-detail hygiene: `dispatch_event` caps every returned detail string at
`_DETAIL_MAX_CHARS`. Handler notes/errors can embed payload-derived text and
the API layer echoes `detail` in the webhook ack (and stores it on the
`webhook_events` row); the full error text is always in the server logs.

Transaction boundary: like every service here, handlers flush but NEVER
commit — the API layer (or the reconcile sweep's caller) owns the commit.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import settings
from app.db import utcnow
from app.logging import get_logger, request_id_ctx
from app.models import (
    CONNECTION_STATE_SINGLETON_ID,
    AuditLog,
    ConnectionState,
    Payment,
    PaymentEvent,
    RecoveryAction,
    RecoveryOpportunity,
)
from app.models.base import (
    ENVIRONMENT_RESEARCH,
    RAZORPAY_SOURCE_SYSTEM,
    SIMULATOR_SOURCE_SYSTEM,
    SOURCE_TYPE_RAZORPAY_TEST,
    SOURCE_TYPE_SIMULATOR,
)
from app.ports import ActionType, RecoveryStatus
from app.services.policy import audit
from app.services.razorpay.factory import use_simulator

logger = get_logger(__name__)

# Action states from which a verification event may still move the action.
_OPEN_ACTION_STATES = (
    RecoveryStatus.EXECUTING,
    RecoveryStatus.VERIFYING,
    RecoveryStatus.FAILED,  # failed is not terminal: late capture may still win
)

# Cap on the `detail` text `dispatch_event` returns — the API layer echoes it
# in the webhook ack and stores it on the event row, and handler notes/errors
# can embed payload-derived text (attacker-influenceable). The full text is
# always available server-side in the structured logs; 200 chars keeps the
# exception type plus a useful prefix.
_DETAIL_MAX_CHARS = 200


def _cap_detail(detail: str | None) -> str | None:
    if detail is not None and len(detail) > _DETAIL_MAX_CHARS:
        return detail[:_DETAIL_MAX_CHARS] + "...[truncated]"
    return detail


def dispatch_event(
    db: Session, event_type: str, payload: dict[str, Any]
) -> tuple[bool, str | None]:
    """Run one stored event through the handler registry.

    Returns `(processed, detail)` — the caller persists them on the
    `webhook_events` row. `processed=False` keeps the event reconcilable.
    `detail` is capped at `_DETAIL_MAX_CHARS` (see above).
    """
    _stamp_connection_webhook_activity(db)
    handler = EVENT_HANDLERS.get(event_type)
    if handler is None:
        return True, _cap_detail(f"event {event_type!r} stored; no handler registered")
    try:
        detail = handler(db, payload)
    except Exception as exc:  # keep the stored event; reconcile later
        db.rollback()  # also undoes the activity stamp above
        logger.exception(
            "webhook handler failed", extra={"event_type": event_type}
        )
        # Re-stamp: the delivery WAS verified even though the handler failed.
        _stamp_connection_webhook_activity(db)
        return False, _cap_detail(f"handler error: {type(exc).__name__}: {exc}")
    return detail is None, _cap_detail(detail)


def _stamp_connection_webhook_activity(db: Session) -> None:
    """Stamp `connection_state.last_webhook_at` for a verified delivery.

    Only a deployment wired to the REAL Razorpay gateway earns the stamp —
    a simulated-gateway delivery must never fake real-connection webhook
    activity (same predicate as `_webhook_provenance`). Flush-only; the
    caller commits. Reconciled re-runs stamp like live ones — a reprocessed
    event behaves bit-for-bit like a live one by design (module docstring).
    """
    if use_simulator(settings):
        return
    state = db.get(ConnectionState, CONNECTION_STATE_SINGLETON_ID)
    if state is None:
        state = ConnectionState(id=CONNECTION_STATE_SINGLETON_ID)
        db.add(state)
    state.last_webhook_at = utcnow()
    db.flush()


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
        hold = _link_paid_verification_hold(action, link)
        if hold is not None:
            _flag_verification_hold(db, action, link, hold)
        else:
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


def _webhook_provenance(entity: dict[str, Any]) -> dict[str, Any]:
    """Provenance fields for a webhook-derived event row: tag it by the
    gateway the deployment is actually wired to (`factory.use_simulator` is
    the single source of truth, mirrored by the webhook ingress' own
    `source` stamp). A simulated-gateway delivery must stay honest
    'simulator'; only real Razorpay Test Mode traffic earns 'razorpay_test'.
    """
    if use_simulator(settings):
        return {
            "source_type": SOURCE_TYPE_SIMULATOR,
            "source_system": SIMULATOR_SOURCE_SYSTEM,
            "external_id": entity.get("id"),
        }
    return {
        "source_type": SOURCE_TYPE_RAZORPAY_TEST,
        "source_system": RAZORPAY_SOURCE_SYSTEM,
        "external_id": entity.get("id"),
    }


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
            **_webhook_provenance(entity),
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
            environment=action.environment or ENVIRONMENT_RESEARCH,
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


def _as_int(value: Any) -> int | None:
    """Payload int or None — JSON `true` is a bool (an int subclass), and
    NaN/Infinity arrive as floats; neither is a paise amount."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bounded(value: Any, limit: int) -> str | None:
    """Short display form for a payload-derived string field (last_error and
    audit details must not carry unbounded attacker-influenced text)."""
    if value is None:
        return None
    return str(value)[:limit]


def _link_paid_verification_hold(
    action: RecoveryAction, link: dict[str, Any]
) -> dict[str, Any] | None:
    """Cross-check a `payment_link.paid` entity against the linked action.

    Returns None when the payload verifies (exact amount, matching currency,
    fully paid). Otherwise a details dict whose `reason` is one of
    `amount_unverifiable` | `amount_mismatch` | `currency_mismatch` |
    `partial_payment` — the action must NOT be marked RECOVERED.
    """
    amount = _as_int(link.get("amount"))
    amount_paid = _as_int(link.get("amount_paid"))
    details: dict[str, Any] = {
        "expected_paise": action.amount_paise,
        "actual_paise": amount,
        "expected_currency": action.currency,
        "actual_currency": _bounded(link.get("currency"), 16),
        "amount_paid_paise": amount_paid,
        "link_status": _bounded(link.get("status"), 32),
    }
    if amount is None:
        # Fail closed: without an integer amount the payload cannot prove the
        # action's amount was paid.
        return details | {"reason": "amount_unverifiable"}
    if amount != action.amount_paise:
        return details | {"reason": "amount_mismatch"}
    currency = link.get("currency")
    if currency is not None and currency != (action.currency or "INR"):
        return details | {"reason": "currency_mismatch"}
    # Partial payments never count as recovered: the link must be fully paid.
    # (`payment_link.partially_paid` is a distinct event type with no handler
    # here; this guards a `payment_link.paid` whose entity disagrees.)
    if link.get("status") == "partial_paid" or (
        amount_paid is not None and amount_paid < amount
    ):
        return details | {"reason": "partial_payment"}
    return None


def _flag_verification_hold(
    db: Session,
    action: RecoveryAction,
    link: dict[str, Any],
    details: dict[str, Any],
) -> None:
    """Hold a linked action on a failed amount/currency cross-check: do NOT
    mark RECOVERED, keep the current open status, surface the hold in
    `last_error`, and append a `verification.amount_mismatch` audit row with
    expected vs actual. Flush-only; the caller's transaction commits."""
    reason = str(details["reason"])
    action.last_error = (
        f"payment_link.paid verification hold ({reason}): "
        f"expected {details['expected_paise']} {details['expected_currency']}, "
        f"link amount={details['actual_paise']} "
        f"currency={details['actual_currency']} "
        f"amount_paid={details['amount_paid_paise']} "
        f"status={details['link_status']}"
    )
    entry = audit.record(
        db,
        actor="system:webhook",
        action="verification.amount_mismatch",
        entity_type="recovery_action",
        entity_id=action.id,
        details={
            "trigger": "payment_link.paid",
            **details,
            "link_id": _bounded(link.get("id"), 64),
            "reference_id": _bounded(link.get("reference_id"), 64),
            "held_status": action.status.value,
        },
    )
    entry.environment = action.environment or ENVIRONMENT_RESEARCH
    logger.warning(
        "payment_link.paid verification hold",
        extra={
            "action_id": action.id,
            "reason": reason,
            "expected_paise": details["expected_paise"],
            "actual_paise": details["actual_paise"],
        },
    )


__all__ = ["EVENT_HANDLERS", "dispatch_event"]

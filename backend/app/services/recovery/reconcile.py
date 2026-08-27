"""Operator-triggered reconciliation sweep (ADR 0011).

Two drifts are repaired here, deterministically and idempotently:

1. `recovery_actions` stuck in UNKNOWN (a gateway mutation was sent exactly
   once but the outcome was ambiguous — timeout/5xx). Resolution is GET-only:
   each action goes through `RecoveryExecutor.resolve`, which re-queries
   gateway truth (`fetch_order` / `fetch_payment`) and transitions to
   RECOVERED only on positive evidence — never a blind retry, never a guess.
2. `webhook_events` with `processed=false` (the handler raised or could not
   resolve the event at intake time — e.g. the payment arrived after the
   webhook). Each stored event is re-run through the SAME handler registry
   (`webhook_handlers.dispatch_event`) that live intake uses, so a
   reprocessed event behaves bit-for-bit like a live one.

Transaction boundary — documented exception to "services never commit":
this sweep COMMITS per repaired unit. `dispatch_event` rolls the whole
session back when a handler fails, so batching the sweep into one commit
would let one bad event silently undo earlier repairs. Per-unit commits make
every repair independently durable and the sweep safely re-runnable; the
sweep's own audit row is committed last. A second sweep over a clean
database is a no-op apart from that audit row.

The sweep is operator-triggered (POST /api/v1/recovery/reconcile) — there is
no background scheduler in v1; the worker tier is P2 (ADR 0009/0011).
"""

from dataclasses import asdict, dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import ids
from app.db import utcnow
from app.logging import get_logger
from app.models import RecoveryAction, WebhookEvent
from app.ports import PaymentGateway, RecoveryStatus
from app.services.policy import audit
from app.services.recovery.executor import RecoveryExecutor
from app.services.recovery.webhook_handlers import dispatch_event

logger = get_logger(__name__)


@dataclass(frozen=True)
class ReconcileReport:
    """What one sweep did. `webhooks_reprocessed` counts events that are now
    processed=true; `webhooks_still_failing` counts events the sweep re-ran
    that remain unprocessed (handler error or still-unresolvable note)."""

    sweep_id: str
    unknown_scanned: int = 0
    resolved: int = 0
    still_unknown: int = 0
    webhooks_reprocessed: int = 0
    webhooks_still_failing: int = 0


def run_reconciliation(
    db: Session,
    gateway: PaymentGateway,
    *,
    actor: str,
) -> ReconcileReport:
    """Run one idempotent sweep. See the module docstring for semantics."""
    sweep_id = ids.new_id("rcn_")
    executor = RecoveryExecutor(db, gateway)

    # (i) UNKNOWN recovery actions -> GET-only gateway truth re-query.
    unknown_actions = list(
        db.scalars(
            sa.select(RecoveryAction)
            .where(RecoveryAction.status == RecoveryStatus.UNKNOWN)
            .order_by(RecoveryAction.created_at, RecoveryAction.id)
        )
    )
    resolved = 0
    still_unknown = 0
    for action in unknown_actions:
        try:
            executor.resolve(action.id, actor=actor)
            db.commit()  # per-unit durability: later failures can't undo this
        except Exception as exc:  # one bad action must not abort the sweep
            db.rollback()
            still_unknown += 1
            logger.warning(
                "reconcile: resolve failed",
                extra={"action_id": action.id, "error": f"{type(exc).__name__}: {exc}"},
            )
            continue
        if action.status is RecoveryStatus.UNKNOWN:
            still_unknown += 1
        else:
            resolved += 1

    # (ii) failed webhook events -> re-run through the same handler registry.
    failed_events = list(
        db.scalars(
            sa.select(WebhookEvent)
            .where(WebhookEvent.processed.is_(False))
            .order_by(WebhookEvent.received_at, WebhookEvent.id)
        )
    )
    webhooks_reprocessed = 0
    webhooks_still_failing = 0
    for event in failed_events:
        processed, detail = dispatch_event(db, event.event_type, event.payload)
        event.processed = processed
        event.processed_at = utcnow()
        event.error = detail
        db.commit()  # per-unit durability (dispatch_event may have rolled back)
        if processed:
            webhooks_reprocessed += 1
        else:
            webhooks_still_failing += 1

    report = ReconcileReport(
        sweep_id=sweep_id,
        unknown_scanned=len(unknown_actions),
        resolved=resolved,
        still_unknown=still_unknown,
        webhooks_reprocessed=webhooks_reprocessed,
        webhooks_still_failing=webhooks_still_failing,
    )
    audit.record(
        db,
        actor=actor,
        action="recovery.reconcile",
        entity_type="recovery_reconcile",
        entity_id=sweep_id,
        details=asdict(report),
    )
    db.commit()
    logger.info("reconciliation sweep complete", extra=asdict(report))
    return report


__all__ = ["ReconcileReport", "run_reconciliation"]

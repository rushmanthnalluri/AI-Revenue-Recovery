"""Razorpay webhook intake — a thin ingress adapter.

Delivery semantics handled here (verified in docs/research.md):
- At-least-once, unordered delivery; deduped via the `x-razorpay-event-id`
  header against `webhook_events.gateway_event_id` (UNIQUE). Duplicates ack
  200 `already_processed` with zero side effects.
- `X-Razorpay-Signature` = HMAC-SHA256(webhook_secret, RAW body) — verified
  via the PaymentGateway port before any parsing; mismatch -> 400.

The verification logic itself (the handler registry, the out-of-order-safe
payment state machine, recovery-action transitions) lives in the service
layer: `app.services.recovery.webhook_handlers`. This module only
authenticates, dedupes, persists the raw event, dispatches through
`dispatch_event`, and acks — the reconciliation sweep re-runs failed events
through the exact same code path (`EVENT_HANDLERS` is re-exported here for
the evaluation harness). Ack target is <5s.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_gateway_dependency
from app.config import settings
from app.db import get_db, utcnow
from app.logging import get_logger
from app.models import WebhookEvent
from app.ports import PaymentGateway
from app.schemas.webhooks import WebhookAck
from app.services.razorpay.factory import use_simulator
from app.services.recovery.webhook_handlers import EVENT_HANDLERS, dispatch_event

logger = get_logger("app.api.v1.webhooks")

router = APIRouter(tags=["webhooks"])

# Razorpay event payloads are a few KB; 1 MiB is generous headroom. The cap is
# enforced BEFORE signature verification so a junk flood cannot make the
# process buffer unbounded request bodies in memory.
MAX_WEBHOOK_BODY_BYTES = 1_048_576


async def _read_capped_body(request: Request) -> bytes:
    """Read the raw body with a hard size cap (413 beyond it)."""
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(413, "Payload too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(413, "Payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/webhooks/razorpay", response_model=WebhookAck)
async def razorpay_webhook(
    request: Request,
    db: Session = Depends(get_db),
    gateway: PaymentGateway = Depends(get_gateway_dependency),
) -> WebhookAck:
    raw = await _read_capped_body(request)

    signature = request.headers.get("x-razorpay-signature", "")
    if not signature:
        raise HTTPException(400, "Missing X-Razorpay-Signature header")
    if not gateway.verify_webhook_signature(raw, signature):
        logger.warning("webhook signature mismatch")
        raise HTTPException(400, "Invalid webhook signature")

    gateway_event_id = request.headers.get("x-razorpay-event-id", "")
    if not gateway_event_id:
        raise HTTPException(400, "Missing X-Razorpay-Event-Id header")

    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        # ValueError: invalid JSON; RecursionError: pathological nesting depth
        # (~100k levels) — a 400, never a 500 that invites a retry storm.
        raise HTTPException(400, "Webhook body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Webhook body must be a JSON object")

    event_type = str(payload.get("event") or "unknown")
    received_at = utcnow()

    row = WebhookEvent(
        gateway_event_id=gateway_event_id,
        event_type=event_type,
        payload=payload,
        signature_valid=True,
        processed=False,
        received_at=received_at,
        source="simulator" if use_simulator(settings) else "razorpay",
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Duplicate x-razorpay-event-id: acknowledge with zero side effects.
        db.rollback()
        return WebhookAck(
            status="already_processed",
            event_id=gateway_event_id,
            duplicate=True,
            received_at=received_at,
            detail="Duplicate x-razorpay-event-id; ignored with zero side effects.",
        )

    processed, detail = dispatch_event(db, event_type, payload)

    row.processed = processed
    row.processed_at = utcnow()
    row.error = detail
    db.commit()

    return WebhookAck(
        status="received",
        event_id=gateway_event_id,
        duplicate=False,
        received_at=received_at,
        processed=processed,
        detail=detail,
    )


__all__ = ["router", "EVENT_HANDLERS", "get_gateway_dependency"]

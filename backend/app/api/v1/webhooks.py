"""Razorpay webhook intake. Owner: gateway/webhook agent.

Contract for the implementing agent:
- verify X-Razorpay-Signature via PaymentGateway.verify_webhook_signature
- persist raw payload to webhook_events (gateway_event_id UNIQUE = dedup)
- ack 200 fast (app.schemas.webhooks.WebhookAck); process out-of-band
- rate-limited by middleware (see app.main)
"""

from fastapi import APIRouter, Request

from app.api import not_implemented

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/razorpay")
def razorpay_webhook(request: Request):
    # 501 stub: success shape is app.schemas.webhooks.WebhookAck.
    return not_implemented("razorpay webhook intake")

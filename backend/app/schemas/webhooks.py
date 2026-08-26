"""Webhook schemas — Razorpay inbound events (raw dict payloads; signature
verification happens in the handler via the PaymentGateway port)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class WebhookAck(BaseModel):
    status: str  # received | duplicate | rejected
    event_id: str | None = None
    duplicate: bool = False
    received_at: datetime | None = None
    detail: str | None = None


class WebhookEventView(BaseModel):
    id: str
    gateway_event_id: str
    event_type: str
    signature_valid: bool
    processed: bool
    received_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

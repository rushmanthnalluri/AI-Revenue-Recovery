"""Invariant 4 (fail-closed half): webhook signatures are verified before any
processing, and verification FAILS CLOSED when no secret is configured.

Existing proofs (referenced from docs/payment-invariants.md):
- tests/razorpay/test_webhooks.py — invalid/tampered/missing signature -> 400,
  NOTHING stored (test_invalid_signature_rejected_400_nothing_stored).
- tests/razorpay/test_client.py::test_verify_webhook_signature_fails_closed_without_secret
  — port-level fail-closed.
- tests/security/test_webhook_adversarial.py::TestMalformedWebhookBodies::test_oversized_body_rejected_before_processing
  — the size cap fires before HMAC/parsing.

Gap closed here: the ROUTE-level fail-closed case. With an empty configured
webhook secret, even a request signed with the would-be secret is rejected
400 and nothing is stored or processed — a misconfigured deployment cannot
silently accept attacker-forged events.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import sqlalchemy as sa
from fastapi.testclient import TestClient

import app.models as models
from app.api.deps import get_gateway_dependency
from app.db import get_db
from app.main import create_app
from app.services.razorpay.simulated import SimulatedPaymentGateway


def test_route_rejects_every_delivery_when_no_secret_configured(db_session, make_payment):
    gateway = SimulatedPaymentGateway(success_rate=1.0, webhook_secret="")
    app = create_app()

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gateway_dependency] = lambda: gateway

    payment = make_payment(gateway_payment_id="pay_nosecret", status="created")
    body = json.dumps(
        {
            "entity": "event",
            "event": "payment.captured",
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_nosecret",
                        "entity": "payment",
                        "amount": 50000,
                        "currency": "INR",
                        "status": "captured",
                        "captured": True,
                    }
                }
            },
            "created_at": 1700000000,
        }
    ).encode()
    # Signed with SOME secret — irrelevant: verification must fail closed.
    signature = hmac.new(b"whsec_anything", body, hashlib.sha256).hexdigest()

    with TestClient(app) as client:
        r = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "x-razorpay-signature": signature,
                "x-razorpay-event-id": "evt_no_secret",
            },
        )
    assert r.status_code == 400
    # zero processing: nothing stored, payment untouched
    n = db_session.scalar(sa.select(sa.func.count()).select_from(models.WebhookEvent))
    assert n == 0
    db_session.refresh(payment)
    assert payment.status == "created"
    assert payment.captured is False

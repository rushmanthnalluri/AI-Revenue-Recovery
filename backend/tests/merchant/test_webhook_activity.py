"""Verified webhooks stamp `connection_state.last_webhook_at` — only for
deployments wired to the REAL Razorpay gateway (never in simulator mode).
Webhook secrets/keys below are fixtures.
"""

import hashlib
import hmac
import json

import pytest
from sqlalchemy.orm import Session

import app.models as models
from app.api.deps import get_gateway_dependency
from app.config import settings
from app.services.razorpay.client import RazorpayGateway
from app.services.recovery.webhook_handlers import dispatch_event
from tests.merchant.conftest import KEY_ID, KEY_SECRET

WH_SECRET = "whsec_fixture_secret"


@pytest.fixture()
def real_mode(monkeypatch) -> None:
    """Pretend the deployment is wired to the real gateway (fixtures only)."""
    monkeypatch.setattr(settings, "SIMULATION_MODE", False)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", KEY_ID)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", KEY_SECRET)


def test_dispatch_stamps_last_webhook_at_in_real_mode(
    db_session: Session, real_mode: None
) -> None:
    processed, _ = dispatch_event(db_session, "unknown.fixture", {"payload": {}})
    assert processed is True
    state = db_session.get(models.ConnectionState, "merchant")
    assert state is not None
    assert state.last_webhook_at is not None


def test_dispatch_stamps_even_when_handler_fails(
    db_session: Session, real_mode: None, monkeypatch
) -> None:
    """A verified delivery counts even if the handler errors (the stamp is
    re-applied after the handler's rollback)."""
    from app.services.recovery import webhook_handlers

    def _boom(db, payload):  # fixture handler that always fails
        raise RuntimeError("fixture handler failure")

    monkeypatch.setitem(webhook_handlers.EVENT_HANDLERS, "payment.captured", _boom)
    processed, detail = dispatch_event(
        db_session,
        "payment.captured",
        {"payload": {"payment": {"entity": {"id": "pay_FixUnknown1"}}}},
    )
    assert processed is False
    assert "fixture handler failure" in (detail or "")
    state = db_session.get(models.ConnectionState, "merchant")
    assert state is not None
    assert state.last_webhook_at is not None


def test_dispatch_does_not_stamp_in_simulator_mode(
    db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "SIMULATION_MODE", True)
    dispatch_event(db_session, "unknown.fixture", {"payload": {}})
    state = db_session.get(models.ConnectionState, "merchant")
    assert state is None or state.last_webhook_at is None


def test_verified_webhook_http_stamps_last_webhook_at(
    client, db_session: Session, real_mode: None
) -> None:
    """End-to-end: HMAC-verified POST /webhooks/razorpay stamps the cursor."""
    gateway = RazorpayGateway(
        key_id=KEY_ID, key_secret=KEY_SECRET, webhook_secret=WH_SECRET
    )
    client.app.dependency_overrides[get_gateway_dependency] = lambda: gateway

    body = json.dumps(
        {
            "entity": "event",
            "event": "payment.captured",
            "payload": {"payment": {"entity": {"id": "pay_FixUnknown2"}}},
            "created_at": 1_700_000_000,
        }
    ).encode()
    signature = hmac.new(WH_SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "x-razorpay-signature": signature,
            "x-razorpay-event-id": "evt_fixture_1",
        },
    )
    assert resp.status_code == 200
    state = db_session.get(models.ConnectionState, "merchant")
    assert state is not None
    assert state.last_webhook_at is not None

    # ... and the stamp is visible on the merchant connection API.
    conn = client.get("/api/v1/merchant/connection").json()
    assert conn["last_webhook_at"] is not None

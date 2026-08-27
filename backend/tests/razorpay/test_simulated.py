"""SimulatedPaymentGateway tests (SIMULATION twin): determinism, dedupe
behavior mirroring Razorpay semantics, incident context, signature roundtrip."""

import pytest

from app.ports import PaymentGateway
from app.services.razorpay.errors import (
    GatewayBadRequestError,
    GatewayNotFoundError,
    GatewayTransientError,
)
from app.services.razorpay.simulated import SimulatedPaymentGateway

KEYS = [f"act_key{i:02d}" for i in range(8)]


def _links(gw: SimulatedPaymentGateway) -> list[dict]:
    return [
        gw.create_payment_link(amount_paise=1000 * (i + 1), idempotency_key=k)
        for i, k in enumerate(KEYS)
    ]


def test_protocol_conformance():
    assert isinstance(SimulatedPaymentGateway(), PaymentGateway)


def test_determinism_same_seed_same_outcomes():
    a = SimulatedPaymentGateway(seed=7)
    b = SimulatedPaymentGateway(seed=7)
    links_a = _links(a)
    links_b = _links(b)
    assert [l["status"] for l in links_a] == [l["status"] for l in links_b]
    assert links_a == links_b  # byte-identical payloads, incl. timestamps


def test_determinism_reproducible_within_seed():
    gw1 = SimulatedPaymentGateway(seed=123)
    gw2 = SimulatedPaymentGateway(seed=123)
    out1 = [gw1.create_payment_link(amount_paise=5000, idempotency_key=k)["status"] for k in KEYS]
    out2 = [gw2.create_payment_link(amount_paise=5000, idempotency_key=k)["status"] for k in KEYS]
    assert out1 == out2
    assert set(out1) <= {"created", "paid"}


def test_success_rate_one_always_pays():
    gw = SimulatedPaymentGateway(seed=1, success_rate=1.0)
    link = gw.create_payment_link(amount_paise=5000, idempotency_key="act_a")
    assert link["status"] == "paid"
    assert link["amount_paid"] == 5000
    payment = gw.fetch_payment(link["payments"][0]["id"])
    assert payment["status"] == "captured"
    assert payment["captured"] is True


def test_success_rate_zero_never_pays_with_failure_telemetry():
    gw = SimulatedPaymentGateway(seed=1, success_rate=0.0)
    link = gw.create_payment_link(amount_paise=5000, idempotency_key="act_b")
    assert link["status"] == "created"
    assert link["amount_paid"] == 0
    payment = gw.fetch_payment(link["payments"][0]["id"])
    assert payment["status"] == "failed"
    assert payment["error_reason"]  # error taxonomy populated


def test_incident_outage_raises_transient():
    gw = SimulatedPaymentGateway(seed=1, incident={"outage": True})
    with pytest.raises(GatewayTransientError):
        gw.create_payment_link(amount_paise=5000, idempotency_key="act_c")
    with pytest.raises(GatewayTransientError):
        gw.create_order(amount_paise=5000)


def test_incident_forces_error_reason():
    gw = SimulatedPaymentGateway(
        seed=1, success_rate=0.0, incident={"error_reason": "bank_technical_error"}
    )
    link = gw.create_payment_link(amount_paise=5000, idempotency_key="act_d")
    payment = gw.fetch_payment(link["payments"][0]["id"])
    assert payment["error_reason"] == "bank_technical_error"


def test_order_receipt_dedupe_mirrors_razorpay():
    gw = SimulatedPaymentGateway(seed=1)
    gw.create_order(amount_paise=100, receipt="act_dup")
    with pytest.raises(GatewayBadRequestError, match="same receipt"):
        gw.create_order(amount_paise=200, receipt="act_dup")


def test_payment_link_reference_id_dedupe_mirrors_razorpay():
    gw = SimulatedPaymentGateway(seed=1)
    gw.create_payment_link(amount_paise=100, idempotency_key="act_dup_link")
    with pytest.raises(GatewayBadRequestError, match="reference id"):
        gw.create_payment_link(amount_paise=200, idempotency_key="act_dup_link")


def test_fetch_unknown_entities_raise_not_found():
    gw = SimulatedPaymentGateway(seed=1)
    with pytest.raises(GatewayNotFoundError):
        gw.fetch_payment("pay_nope")
    with pytest.raises(GatewayNotFoundError):
        gw.fetch_order("order_nope")


def test_build_event_signature_roundtrip():
    gw = SimulatedPaymentGateway(seed=1, webhook_secret="s3cret")
    link = gw.create_payment_link(amount_paise=100, idempotency_key="act_e")
    payment = gw.fetch_payment(link["payments"][0]["id"])
    body, signature, event_id = gw.build_event("payment.captured", payment)
    assert event_id.startswith("evt_sim")
    assert gw.verify_webhook_signature(body, signature) is True
    assert gw.verify_webhook_signature(body + b"x", signature) is False


def test_subscription_created_deterministically():
    gw = SimulatedPaymentGateway(seed=1)
    sub = gw.create_subscription(plan_id="plan_x", total_count=6, idempotency_key="act_s")
    assert sub["id"].startswith("sub_sim")
    assert sub["status"] in {"active", "pending"}
    assert sub["notes"]["gateway_request_id"] == "act_s"

"""Real-adapter tests using httpx.MockTransport — no network.

Covers: basic-auth header, order/link/subscription request shapes, typed error
mapping, timeout/5xx -> ambiguous (UNKNOWN) with NO blind retry on mutations,
backoff retries only on idempotent GETs, and webhook signature verification.
"""

import base64
import hashlib
import hmac
import json

import httpx
import pytest

from app.ports import PaymentGateway
from app.services.razorpay.client import RazorpayGateway
from app.services.razorpay.errors import (
    GatewayAuthenticationError,
    GatewayBadRequestError,
    GatewayNotFoundError,
    GatewayResponseError,
    GatewayServerError,
    GatewayTransientError,
)

KEY_ID = "rzp_test_abc123"
KEY_SECRET = "secret_xyz"
EXPECTED_AUTH = "Basic " + base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()


def make_gateway(handler, **kw):
    kw.setdefault("sleep", lambda _s: None)  # no real sleeping in tests
    return RazorpayGateway(
        key_id=KEY_ID,
        key_secret=KEY_SECRET,
        transport=httpx.MockTransport(handler),
        **kw,
    )


def order_payload(**over):
    payload = {
        "id": "order_Kaaj123",
        "entity": "order",
        "amount": 50000,
        "amount_paid": 0,
        "amount_due": 50000,
        "currency": "INR",
        "receipt": "act_abc",
        "status": "created",
        "attempts": 0,
        "notes": {},
        "created_at": 1700000000,
    }
    payload.update(over)
    return payload


def test_protocol_conformance():
    gw = make_gateway(lambda req: httpx.Response(200, json=order_payload()))
    assert isinstance(gw, PaymentGateway)


def test_basic_auth_header_and_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        seen["method"] = request.method
        return httpx.Response(200, json=order_payload())

    gw = make_gateway(handler)
    gw.create_order(amount_paise=50000, receipt="act_abc")
    assert seen["authorization"] == EXPECTED_AUTH
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/orders"


def test_create_order_request_body_and_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=order_payload())

    gw = make_gateway(handler)
    out = gw.create_order(
        amount_paise=50000,
        currency="INR",
        notes={"incident": "inc_1"},
        idempotency_key="act_0123456789abcdef",
    )
    assert seen["body"] == {
        "amount": 50000,
        "currency": "INR",
        "receipt": "act_0123456789abcdef",  # idempotency_key -> receipt dedupe
        "notes": {"incident": "inc_1"},
    }
    assert out["id"] == "order_Kaaj123"
    assert out["status"] == "created"


def test_create_order_receipt_truncated_to_40_chars():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=order_payload())

    gw = make_gateway(handler)
    gw.create_order(amount_paise=100, receipt="r" * 60)
    assert seen["body"]["receipt"] == "r" * 40


def test_create_payment_link_uses_reference_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "id": "plink_Kaaj123",
                "entity": "payment_link",
                "status": "created",
                "amount": 50000,
                "reference_id": "act_linkkey",
                "short_url": "https://rzp.io/i/abc",
            },
        )

    gw = make_gateway(handler)
    out = gw.create_payment_link(
        amount_paise=50000,
        idempotency_key="act_linkkey",
        customer={"name": "A", "email": "a@b.c"},
        description="recovery link",
    )
    assert seen["path"] == "/v1/payment_links"
    assert seen["body"]["reference_id"] == "act_linkkey"
    assert seen["body"]["amount"] == 50000
    assert seen["body"]["customer"] == {"name": "A", "email": "a@b.c"}
    assert out["id"] == "plink_Kaaj123"


def test_create_subscription_body_and_no_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"id": "sub_Kaaj123", "entity": "subscription", "status": "created"}
        )

    gw = make_gateway(handler)
    out = gw.create_subscription(
        plan_id="plan_1", customer_id="cust_1", total_count=12, idempotency_key="act_sub1"
    )
    assert calls == [
        {
            "plan_id": "plan_1",
            "total_count": 12,
            "customer_id": "cust_1",
            "notes": {"gateway_request_id": "act_sub1"},
        }
    ]
    assert out["id"] == "sub_Kaaj123"


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (400, GatewayBadRequestError),
        (401, GatewayAuthenticationError),
        (403, GatewayAuthenticationError),
        (404, GatewayNotFoundError),
    ],
)
def test_error_mapping(status, exc_type):
    envelope = {
        "error": {
            "code": "BAD_REQUEST_ERROR",
            "description": "something failed",
            "source": "customer",
            "step": "payment_authentication",
            "reason": "incorrect_otp",
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=envelope)

    gw = make_gateway(handler)
    with pytest.raises(exc_type) as ei:
        gw.create_order(amount_paise=100)
    err = ei.value
    assert err.status_code == status
    assert err.code == "BAD_REQUEST_ERROR"
    assert err.source == "customer"
    assert err.step == "payment_authentication"
    assert err.reason == "incorrect_otp"
    assert "something failed" in str(err)


def test_timeout_on_mutation_raises_transient_with_single_send():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ConnectTimeout("boom", request=request)

    gw = make_gateway(handler)
    with pytest.raises(GatewayTransientError):
        gw.create_order(amount_paise=100, idempotency_key="act_x")
    # NEVER blind-retry a mutating call: exactly one request went out.
    assert calls == ["/v1/orders"]


def test_server_error_on_mutation_raises_with_single_send():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500, json={"error": {"code": "GATEWAY_ERROR", "description": "oops"}})

    gw = make_gateway(handler)
    with pytest.raises(GatewayServerError):
        gw.create_payment_link(amount_paise=100, idempotency_key="act_y")
    assert calls == ["/v1/payment_links"]


def test_idempotent_get_retries_with_backoff_then_succeeds():
    calls = []
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) < 3:
            return httpx.Response(500, json={"error": {"description": "bad"}})
        return httpx.Response(200, json={"id": "pay_Kaaj123", "entity": "payment", "status": "captured"})

    gw = make_gateway(handler, sleep=delays.append, backoff_base_seconds=0.25)
    out = gw.fetch_payment("pay_Kaaj123")
    assert out["status"] == "captured"
    assert calls == ["/v1/payments/pay_Kaaj123"] * 3
    assert delays == [0.25, 0.5]  # exponential backoff between retries


def test_idempotent_get_retry_exhaustion_raises():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503, json={"error": {"description": "down"}})

    gw = make_gateway(handler, max_retries=3)
    with pytest.raises(GatewayServerError):
        gw.fetch_order("order_missing")
    assert calls == ["/v1/orders/order_missing"] * 3


def test_malformed_success_response_raises_response_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    gw = make_gateway(handler)
    with pytest.raises(GatewayResponseError):
        gw.fetch_payment("pay_x")


def test_success_response_missing_id_raises_response_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"entity": "payment"})

    gw = make_gateway(handler)
    with pytest.raises(GatewayResponseError):
        gw.fetch_payment("pay_x")


# --- webhook signature verification ----------------------------------------

SECRET = "whsec_unit_test"


def _sig(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _gw() -> RazorpayGateway:
    return make_gateway(lambda req: httpx.Response(200, json={"id": "x"}), webhook_secret=SECRET)


def test_verify_webhook_signature_valid():
    body = b'{"event":"payment.captured"}'
    assert _gw().verify_webhook_signature(body, _sig(body)) is True


def test_verify_webhook_signature_invalid():
    body = b'{"event":"payment.captured"}'
    assert _gw().verify_webhook_signature(body, "deadbeef") is False


def test_verify_webhook_signature_tampered_body():
    body = b'{"event":"payment.captured"}'
    sig = _sig(body)
    tampered = b'{"event":"payment.captured","amount":1}'
    assert _gw().verify_webhook_signature(tampered, sig) is False


def test_verify_webhook_signature_fails_closed_without_secret():
    gw = _gw()
    gw._webhook_secret = ""
    body = b"{}"
    assert gw.verify_webhook_signature(body, _sig(body)) is False

"""Guarantee 1: no secret leakage — canary sweep.

Seeds the flow with canary secrets and asserts the canaries appear NOWHERE in:
API responses (incl. error envelopes and 500s), agent tool outputs/report
JSON, audit details JSON, webhook_events stored payloads, recovery_action
last_error fields, or captured structured-log output. Log redaction is
unit-tested for authorization-header and secret-shaped keys.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

import httpx
import sqlalchemy as sa

import app.models as models
from app.api.deps import get_gateway_dependency
from app.config import settings
from app.logging import JsonFormatter, redact
from app.services.razorpay.client import RazorpayGateway

CANARY_KEY_SECRET = "canary-secret-123"
CANARY_WEBHOOK_SECRET = "canary-whsec-456"
CANARY_API_KEY = "canary-api-key-789"
CANARY_OPENAI = "canary-openai-000"
CANARIES = [CANARY_KEY_SECRET, CANARY_WEBHOOK_SECRET, CANARY_API_KEY, CANARY_OPENAI]


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _assert_no_canary(haystack: str, where: str) -> None:
    for canary in CANARIES:
        assert canary not in haystack, f"canary leaked into {where}"


class TestCanarySweep:
    def test_seeded_flow_leaks_no_canary(
        self, client, db_session, make_payment, make_opportunity, monkeypatch, caplog
    ):
        # Seed every secret the deployment holds with a canary value.
        monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_canary")
        monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", CANARY_KEY_SECRET)
        monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", CANARY_WEBHOOK_SECRET)
        monkeypatch.setattr(settings, "API_KEY", CANARY_API_KEY)
        monkeypatch.setattr(settings, "OPENAI_API_KEY", CANARY_OPENAI)

        # The gateway carries the canary secrets in its Basic-auth header; a
        # 401 from the gateway exercises the auth-error path end to end.
        def gateway_401(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={
                    "error": {
                        "code": "AUTHENTICATION_ERROR",
                        "description": "Authentication failed",
                    }
                },
            )

        gateway = RazorpayGateway(
            key_id="rzp_test_canary",
            key_secret=CANARY_KEY_SECRET,
            webhook_secret=CANARY_WEBHOOK_SECRET,
            transport=httpx.MockTransport(gateway_401),
        )
        client.app.dependency_overrides[get_gateway_dependency] = lambda: gateway

        capture = _LogCapture()
        logging.getLogger().addHandler(capture)
        responses: list[str] = []
        try:
            # (a) webhook with a WRONG signature (attacker guessing).
            body = b'{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_x"}}}}'
            bad_sig = hmac.new(b"wrong", body, hashlib.sha256).hexdigest()
            r = client.post(
                "/webhooks/razorpay",
                content=body,
                headers={"x-razorpay-signature": bad_sig, "x-razorpay-event-id": "evt_c1"},
            )
            responses.append(r.text)
            assert r.status_code == 400

            # (b) webhook with the VALID canary signature.
            good_sig = hmac.new(CANARY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
            r = client.post(
                "/webhooks/razorpay",
                content=body,
                headers={"x-razorpay-signature": good_sig, "x-razorpay-event-id": "evt_c2"},
            )
            responses.append(r.text)
            assert r.status_code == 200

            # (c) mutating route without the key, then with a wrong key.
            responses.append(
                client.post("/api/v1/recovery/reconcile", json={"actor": "a"}).text
            )
            responses.append(
                client.post(
                    "/api/v1/recovery/reconcile",
                    json={"actor": "a"},
                    headers={"X-API-Key": "wrong"},
                ).text
            )

            # (d) execute against the 401 gateway with the CORRECT key.
            payment = make_payment(status="failed")
            opp = make_opportunity(payment=payment)
            strategies = client.get(f"/api/v1/recovery/{opp.id}/plan").json()["strategies"]
            link = next(s for s in strategies if s["action_type"] == "create_payment_link")
            r = client.post(
                f"/api/v1/recovery/{opp.id}/execute",
                json={"strategy_id": link["id"], "actor": "human:ops"},
                headers={"X-API-Key": CANARY_API_KEY},
            )
            responses.append(r.text)
            # (e) read the action back (detail carries last_error/audit refs).
            responses.append(client.get(f"/api/v1/recovery/{opp.id}").text)
            responses.append(client.get("/api/v1/audit").text)

            # (f) agent investigation (heuristic reasoner; OPENAI canary set).
            from app.services.agent.service import AgentService

            AgentService(db_session).investigate(opp.incident_id)
        finally:
            logging.getLogger().removeHandler(capture)

        # --- assertions: the canary appears NOWHERE observable ---------------
        for i, text in enumerate(responses):
            _assert_no_canary(text, f"API response #{i}")

        for row in db_session.scalars(sa.select(models.AuditLog)).all():
            _assert_no_canary(json.dumps(row.details or {}, default=str), f"audit {row.id}")
            _assert_no_canary(row.actor or "", f"audit actor {row.id}")

        for row in db_session.scalars(sa.select(models.AgentReport)).all():
            _assert_no_canary(json.dumps(row.output or {}, default=str), f"agent_report {row.id}")
            _assert_no_canary(json.dumps(row.input or {}, default=str), f"agent_report input {row.id}")
            _assert_no_canary(row.error or "", f"agent_report error {row.id}")

        for row in db_session.scalars(sa.select(models.WebhookEvent)).all():
            _assert_no_canary(json.dumps(row.payload or {}, default=str), f"webhook_event {row.id}")
            _assert_no_canary(row.error or "", f"webhook_event error {row.id}")

        for row in db_session.scalars(sa.select(models.RecoveryAction)).all():
            _assert_no_canary(row.last_error or "", f"action last_error {row.id}")
            _assert_no_canary(
                json.dumps(row.gateway_response or {}, default=str), f"action response {row.id}"
            )

        _assert_no_canary("\n".join(capture.lines), "structured logs")


class TestLogRedaction:
    """Unit coverage for the redactor: authorization headers, secret-shaped
    keys, nested structures, and basic-auth userinfo adjacency."""

    def test_secret_shaped_keys_redacted(self):
        payload = redact(
            {
                "api_key": "k",
                "API_KEY": "k",
                "x-api-key": "k",
                "password": "p",
                "Authorization": "Bearer abc",
                "authorization": "Basic dXNlcjpwYXNz",
                "webhook_secret": "w",
                "key_secret": "s",
                "access_token": "t",
                "signature": "sig",
                "credential": "c",
                "client_secret": "cs",
                "harmless": "visible",
            }
        )
        for key, value in payload.items():
            if key == "harmless":
                assert value == "visible"
            else:
                assert value == "***redacted***", key

    def test_nested_and_listed_structures_redacted(self):
        payload = redact(
            {
                "headers": {"Authorization": "Basic dXNlcjpwYXNz", "Accept": "application/json"},
                "events": [{"webhook_secret": "w"}, {"ok": 1}],
            }
        )
        assert payload["headers"]["Authorization"] == "***redacted***"
        assert payload["headers"]["Accept"] == "application/json"
        assert payload["events"][0]["webhook_secret"] == "***redacted***"
        assert payload["events"][1]["ok"] == 1

    def test_formatter_applies_redaction_to_extra(self):
        formatter = JsonFormatter()
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", (), None)
        record.headers = {"authorization": "Bearer xyz"}
        out = json.loads(formatter.format(record))
        assert out["extra"]["headers"]["authorization"] == "***redacted***"

    def test_gateway_never_logs_credentials(self):
        """The Razorpay adapter holds key material only inside the httpx
        Basic-auth tuple; it must never emit it to logs — even on errors."""
        capture = _LogCapture()
        logging.getLogger("app.services.razorpay.client").addHandler(capture)
        try:
            gw = RazorpayGateway(
                key_id="rzp_test_canary",
                key_secret=CANARY_KEY_SECRET,
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(500, json={"error": {"description": "boom"}})
                ),
                sleep=lambda _: None,
            )
            try:
                gw.fetch_payment("pay_x")
            except Exception:
                pass
            try:
                gw.create_subscription(plan_id="plan_x", idempotency_key="gwr_x")
            except Exception:
                pass
        finally:
            logging.getLogger("app.services.razorpay.client").removeHandler(capture)
        assert CANARY_KEY_SECRET not in "\n".join(capture.lines)


class TestErrorEnvelopeSafety:
    def test_500_envelope_never_echoes_internals(self, client):
        from fastapi.testclient import TestClient

        def boom() -> None:
            raise RuntimeError("canary-secret-123 exploded inside a dependency")

        client.app.dependency_overrides[get_gateway_dependency] = boom
        try:
            # raise_server_exceptions=False: starlette's ServerErrorMiddleware
            # re-raises AFTER producing the response; we want the response.
            with TestClient(client.app, raise_server_exceptions=False) as c:
                r = c.get("/api/v1/recovery/opp_x")
        finally:
            client.app.dependency_overrides.pop(get_gateway_dependency, None)
        assert r.status_code == 500
        body = r.json()
        assert body == {
            "error": {
                "code": "internal_error",
                "message": "Internal server error.",
                "request_id": body["error"]["request_id"],
            }
        }
        assert "canary" not in r.text
        assert "exploded" not in r.text

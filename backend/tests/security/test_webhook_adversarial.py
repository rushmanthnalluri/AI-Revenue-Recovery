"""Attack vectors: adversarial webhook deliveries.

- CONCURRENT duplicate deliveries racing the dedup constraint (the existing
  suite proves sequential duplicates; this proves the race).
- Out-of-order: payment_link.paid arriving BEFORE the recovery action row
  exists (must be stored, acked, and recovered by the reconcile sweep).
- Oversized bodies (10MB) and deeply nested JSON (100k levels) — resource
  exhaustion and a RecursionError that used to escape as a 500.
- Non-standard JSON constants (NaN/Infinity) in a signed payload.

All webhook posts in this package use a REAL HMAC signature unless the test
is specifically about the signature gate.
"""

from __future__ import annotations

import json
import threading

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app.models as models
from app.api.deps import get_gateway_dependency
from app.db import Base, get_db, utcnow
from app.main import create_app
from app.ports import ActionType, RecoveryStatus
from app.services.razorpay.simulated import SimulatedPaymentGateway

from tests.security.conftest import WH_SECRET


def _payment_captured_body(gateway_payment_id: str) -> bytes:
    return json.dumps(
        {
            "entity": "event",
            "event": "payment.captured",
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": gateway_payment_id,
                        "entity": "payment",
                        "amount": 50000,
                        "currency": "INR",
                        "status": "captured",
                        "method": "upi",
                        "captured": True,
                    }
                }
            },
            "created_at": 1700000000,
        }
    ).encode()


class TestConcurrentDuplicateDeliveries:
    """Two deliveries with the same x-razorpay-event-id racing: exactly one
    may produce side effects. The UNIQUE constraint is the guard; the loser
    must ack already_processed with zero side effects — not crash."""

    def test_racing_duplicate_deliveries_exactly_one_side_effect(
        self, tmp_path, make_payment
    ):
        # File-backed DB so two request threads get genuinely independent
        # sessions/connections (a StaticPool in-memory DB would serialize on
        # one shared connection and prove nothing about the constraint).
        db_file = tmp_path / "race.db"
        engine = sa.create_engine(
            f"sqlite:///{db_file}", connect_args={"check_same_thread": False}, poolclass=NullPool
        )

        @sa.event.listens_for(engine, "connect")
        def _busy_timeout(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            # Readers/writers wait instead of erroring with 'database is
            # locked' — mirrors production Postgres semantics where the UNIQUE
            # constraint, not a lock error, decides the race.
            cur.execute("PRAGMA busy_timeout=10000")
            cur.close()

        Base.metadata.create_all(engine)
        TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        seed = TestingSession()
        merchant = models.Merchant(name="race merchant")
        seed.add(merchant)
        seed.flush()
        payment = models.Payment(
            merchant_id=merchant.id,
            amount_paise=50_000,
            status="failed",
            gateway_payment_id="pay_race_target",
        )
        seed.add(payment)
        seed.commit()
        seed.close()

        gateway = SimulatedPaymentGateway(webhook_secret=WH_SECRET)
        app = create_app()

        def _override_get_db():
            session = TestingSession()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_gateway_dependency] = lambda: gateway

        body = _payment_captured_body("pay_race_target")
        signature = __import__("hmac").new(
            WH_SECRET.encode(), body, __import__("hashlib").sha256
        ).hexdigest()
        headers = {
            "x-razorpay-signature": signature,
            "x-razorpay-event-id": "evt_race_001",
        }

        results: list[tuple[int, dict]] = []

        def _deliver() -> None:
            # One TestClient per thread: httpx clients are not thread-shared.
            with TestClient(app) as c:
                r = c.post("/webhooks/razorpay", content=body, headers=headers)
                results.append((r.status_code, r.json()))

        threads = [threading.Thread(target=_deliver) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        for t in threads:
            assert not t.is_alive(), "webhook delivery thread hung"

        assert len(results) == 2
        codes = sorted(code for code, _ in results)
        assert codes == [200, 200], f"racing deliveries must both ack 200: {results}"
        statuses = sorted(str(payload.get("status")) for _, payload in results)
        assert statuses == ["already_processed", "received"], (
            f"exactly one delivery may process: {results}"
        )

        check = TestingSession()
        try:
            events = check.scalars(
                sa.select(models.WebhookEvent).where(
                    models.WebhookEvent.gateway_event_id == "evt_race_001"
                )
            ).all()
            assert len(events) == 1, "dedup constraint stored the event exactly once"
            transitions = check.scalars(
                sa.select(models.PaymentEvent).where(
                    models.PaymentEvent.payment_id == payment.id,
                    models.PaymentEvent.to_status == "captured",
                )
            ).all()
            assert len(transitions) == 1, (
                f"exactly one capture side effect may land, saw {len(transitions)}"
            )
            row = check.get(models.Payment, payment.id)
            assert row.status == "captured" and row.captured is True
        finally:
            check.close()
            engine.dispose()


class TestOutOfOrderDeliveries:
    """payment_link.paid arriving BEFORE the action row exists: the handler
    cannot link it, so the event is stored unprocessed; once the action
    exists, the reconcile sweep re-runs the SAME handler and verifies it."""

    def test_link_paid_before_action_row_recovers_via_reconcile(
        self, client, db_session, sign, make_opportunity
    ):
        body = json.dumps(
            {
                "entity": "event",
                "event": "payment_link.paid",
                "contains": ["payment_link"],
                "payload": {
                    "payment_link": {
                        "entity": {
                            "id": "plink_early",
                            "entity": "payment_link",
                            "reference_id": "gwr_arrives_late",
                            "status": "paid",
                            "amount": 100_000,
                            "amount_paid": 100_000,
                        }
                    }
                },
                "created_at": 1700000000,
            }
        ).encode()
        r1 = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "x-razorpay-signature": sign(body),
                "x-razorpay-event-id": "evt_early_link_paid",
            },
        )
        assert r1.status_code == 200
        assert r1.json()["processed"] is False  # unresolvable now, reconcilable

        # The action row arrives later (execution completed after the webhook).
        opp = make_opportunity()
        action = models.RecoveryAction(
            opportunity_id=opp.id,
            incident_id=opp.incident_id,
            action_type=ActionType.CREATE_PAYMENT_LINK,
            status=RecoveryStatus.VERIFYING,
            amount_paise=opp.amount_paise,
            confidence=0.95,
            actor="agent:strategist",
            gateway_request_id="gwr_arrives_late",
            proposed_at=utcnow(),
            executed_at=utcnow(),
        )
        db_session.add(action)
        db_session.commit()

        r2 = client.post(
            "/api/v1/recovery/reconcile",
            json={"actor": "human:ops"},
            headers={"X-API-Key": "dev-key"},
        )
        assert r2.status_code == 200
        assert r2.json()["webhooks_reprocessed"] == 1
        db_session.refresh(action)
        assert action.status is RecoveryStatus.RECOVERED

    def test_event_for_unknown_payment_id_is_stored_not_crashing(
        self, client, sign
    ):
        body = _payment_captured_body("pay_never_seen")
        r = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "x-razorpay-signature": sign(body),
                "x-razorpay-event-id": "evt_unknown_payment",
            },
        )
        assert r.status_code == 200
        assert r.json()["processed"] is False
        assert "unknown payment" in (r.json()["detail"] or "")


class TestMalformedWebhookBodies:
    """Resource-exhaustion and parser-edge deliveries."""

    def _post(self, client, body: bytes, sign, event_id: str, signed: bool = True):
        return client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "x-razorpay-signature": sign(body) if signed else "0" * 64,
                "x-razorpay-event-id": event_id,
            },
        )

    def test_oversized_body_rejected_before_processing(self, client, sign):
        """REGRESSION: the route used to read the body unboundedly into
        memory. A 10MB body must be rejected (413) before HMAC/parsing."""
        big = b'{"event":"payment.captured","pad":"' + b"x" * (10 * 1024 * 1024) + b'"}'
        r = self._post(client, big, sign, "evt_oversized")
        assert r.status_code == 413, f"expected 413 for a 10MB body, got {r.status_code}"
        assert r.json()["error"]["code"] == "payload_too_large"

    def test_deeply_nested_json_rejected_400_not_500(self, client, sign):
        """REGRESSION: json.loads raises RecursionError at ~100k nesting; the
        route caught only ValueError, so a signed deep body produced a 500
        (and a Razorpay retry storm). Must be a clean 400."""
        deep = b"[" * 100_000 + b"]" * 100_000
        r = self._post(client, deep, sign, "evt_deep_nesting")
        assert r.status_code == 400, f"expected 400 for deep nesting, got {r.status_code}"
        assert r.json()["error"]["code"] != "internal_error"

    def test_nonstandard_json_constants_do_not_crash(self, client, sign):
        # Python's json accepts NaN/Infinity; the intake must not 500.
        body = (
            b'{"event":"payment.captured","payload":{"payment":{"entity":'
            b'{"id":"pay_nan","amount":NaN,"captured":true}}},"created_at":Infinity}'
        )
        r = self._post(client, body, sign, "evt_nan_payload")
        assert r.status_code == 200
        assert r.json()["processed"] is False  # unknown payment -> reconcilable

    def test_non_object_json_rejected(self, client, sign):
        body = b'["payment.captured"]'
        r = self._post(client, body, sign, "evt_array_body")
        assert r.status_code == 400

    def test_valid_signature_over_binary_garbage_is_400(self, client, sign):
        body = bytes(range(256)) * 8
        r = self._post(client, body, sign, "evt_binary")
        assert r.status_code == 400

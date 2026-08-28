"""Attack vectors: `payment_link.paid` verification bypass + ack detail echo.

- AMOUNT DRIFT: a signature-valid `payment_link.paid` whose `reference_id`
  anchors to a real recovery action but whose amount/currency does NOT match
  the action must never mark it RECOVERED — the identity anchor proves which
  action, not how much was paid (closes the residual risk "payment_link.paid
  verification trusts reference_id without an amount cross-check").
- PARTIAL PAYMENTS: a link in `partial_paid` status, or with
  `amount_paid < amount`, must never count as recovered.
- ACK ECHO: handler error text and handler notes echoed in the webhook ack
  `detail` (and stored on `webhook_events.error`) are capped — they can
  embed payload-derived, attacker-influenced text; the full text lives in
  the server logs.

All webhook posts in this module use a REAL HMAC signature — the signature
gate is not the subject here; the payload is trusted-but-wrong.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

import app.models as models
from app.db import utcnow
from app.ports import ActionType, RecoveryStatus
from app.services.recovery.webhook_handlers import (
    _DETAIL_MAX_CHARS,
    EVENT_HANDLERS,
    dispatch_event,
)
from app.services.revenue.engine import RevenueService

from tests.security.conftest import API_KEY_HEADER

_OMIT = object()
_TRUNC_SUFFIX = "...[truncated]"


def _link_paid_body(
    reference_id: str,
    *,
    link_id: str = "plink_sec1",
    amount: object = _OMIT,
    amount_paid: object = _OMIT,
    currency: object = _OMIT,
    status: str = "paid",
) -> bytes:
    entity: dict = {
        "id": link_id,
        "entity": "payment_link",
        "reference_id": reference_id,
        "status": status,
    }
    if amount is not _OMIT:
        entity["amount"] = amount
    if amount_paid is not _OMIT:
        entity["amount_paid"] = amount_paid
    if currency is not _OMIT:
        entity["currency"] = currency
    return json.dumps(
        {
            "entity": "event",
            "event": "payment_link.paid",
            "contains": ["payment_link"],
            "payload": {"payment_link": {"entity": entity}},
            "created_at": 1700000000,
        }
    ).encode()


def _post(client, sign, body: bytes, event_id: str):
    return client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "x-razorpay-signature": sign(body),
            "x-razorpay-event-id": event_id,
        },
    )


def _make_link_action(
    db_session,
    make_opportunity,
    *,
    amount_paise: int = 100_000,
    currency: str = "INR",
    gateway_request_id: str,
    status: RecoveryStatus = RecoveryStatus.VERIFYING,
) -> models.RecoveryAction:
    """A VERIFYING create_payment_link action awaiting webhook verification —
    the state the executor leaves after the gateway accepts the link."""
    opp = make_opportunity(amount_paise=amount_paise, currency=currency)
    action = models.RecoveryAction(
        opportunity_id=opp.id,
        incident_id=opp.incident_id,
        action_type=ActionType.CREATE_PAYMENT_LINK,
        status=status,
        amount_paise=amount_paise,
        currency=currency,
        confidence=0.95,
        actor="agent:strategist",
        gateway_request_id=gateway_request_id,
        proposed_at=utcnow(),
        executed_at=utcnow(),
    )
    db_session.add(action)
    db_session.commit()
    return action


def _audits(db_session, action_id: str, action_name: str) -> list[models.AuditLog]:
    return list(
        db_session.scalars(
            sa.select(models.AuditLog).where(
                models.AuditLog.entity_type == "recovery_action",
                models.AuditLog.entity_id == action_id,
                models.AuditLog.action == action_name,
            )
        )
    )


class TestPaymentLinkAmountVerification:
    def test_exact_amount_match_recovers_happy_path_replay(
        self, client, sign, db_session, make_opportunity
    ):
        """No-regression replay of the documented happy path: exact amount +
        currency + full payment recovers the action."""
        action = _make_link_action(
            db_session, make_opportunity, gateway_request_id="gwr_sec_exact"
        )
        body = _link_paid_body(
            "gwr_sec_exact", amount=100_000, amount_paid=100_000, currency="INR"
        )
        r = _post(client, sign, body, "evt_sec_exact")
        assert r.status_code == 200
        assert r.json()["processed"] is True
        db_session.refresh(action)
        assert action.status is RecoveryStatus.RECOVERED
        assert action.verified_at is not None
        assert action.last_error is None
        assert len(_audits(db_session, action.id, "verify_recovered")) == 1
        assert _audits(db_session, action.id, "verification.amount_mismatch") == []

    def test_amount_mismatch_holds_with_audit_and_no_recovered_revenue(
        self, client, sign, db_session, make_opportunity
    ):
        """The core attack: reference_id anchors but the paid amount drifted
        (gateway-side anomaly / forged-by-secret-holder payload). Must hold,
        audit expected-vs-actual, and count ZERO recovered revenue."""
        action = _make_link_action(
            db_session, make_opportunity, gateway_request_id="gwr_sec_drift"
        )
        body = _link_paid_body(
            "gwr_sec_drift", amount=90_000, amount_paid=90_000, currency="INR"
        )
        r = _post(client, sign, body, "evt_sec_drift")
        assert r.status_code == 200
        assert r.json()["processed"] is True  # handled: definitive hold

        db_session.refresh(action)
        assert action.status is RecoveryStatus.VERIFYING, (
            "amount drift must NOT mark the action RECOVERED"
        )
        assert action.verified_at is None
        assert action.last_error is not None
        assert "amount_mismatch" in action.last_error
        assert "100000" in action.last_error and "90000" in action.last_error

        holds = _audits(db_session, action.id, "verification.amount_mismatch")
        assert len(holds) == 1
        details = holds[0].details
        assert details["reason"] == "amount_mismatch"
        assert details["expected_paise"] == 100_000
        assert details["actual_paise"] == 90_000
        assert details["held_status"] == "VERIFYING"
        assert _audits(db_session, action.id, "verify_recovered") == []

        # Recovered revenue is measured from RECOVERED actions only — a held
        # action contributes nothing.
        report = RevenueService(db_session).recovered_revenue(
            datetime(1970, 1, 1, tzinfo=timezone.utc), utcnow() + timedelta(days=1)
        )
        assert report.total_recovered_paise == 0
        assert report.recovered_actions_count == 0

        # The hold is terminal FOR THAT EVENT: the reconcile sweep must not
        # re-run it (no audit spam), and the action stays held.
        r2 = client.post(
            "/api/v1/recovery/reconcile",
            json={"actor": "human:ops"},
            headers=API_KEY_HEADER,
        )
        assert r2.status_code == 200
        assert r2.json()["webhooks_reprocessed"] == 0
        db_session.refresh(action)
        assert action.status is RecoveryStatus.VERIFYING
        assert len(_audits(db_session, action.id, "verification.amount_mismatch")) == 1

    def test_currency_mismatch_holds(
        self, client, sign, db_session, make_opportunity
    ):
        action = _make_link_action(
            db_session, make_opportunity, gateway_request_id="gwr_sec_ccy"
        )
        body = _link_paid_body(
            "gwr_sec_ccy", amount=100_000, amount_paid=100_000, currency="USD"
        )
        r = _post(client, sign, body, "evt_sec_ccy")
        assert r.status_code == 200
        db_session.refresh(action)
        assert action.status is RecoveryStatus.VERIFYING
        assert action.verified_at is None
        holds = _audits(db_session, action.id, "verification.amount_mismatch")
        assert len(holds) == 1
        assert holds[0].details["reason"] == "currency_mismatch"
        assert holds[0].details["expected_currency"] == "INR"
        assert holds[0].details["actual_currency"] == "USD"

    def test_partial_paid_status_not_recovered(
        self, client, sign, db_session, make_opportunity
    ):
        """Partial payments never count as recovered — even when the link's
        full `amount` matches the action (the customer paid only part)."""
        action = _make_link_action(
            db_session, make_opportunity, gateway_request_id="gwr_sec_partial"
        )
        body = _link_paid_body(
            "gwr_sec_partial",
            amount=100_000,
            amount_paid=40_000,
            currency="INR",
            status="partial_paid",
        )
        r = _post(client, sign, body, "evt_sec_partial")
        assert r.status_code == 200
        db_session.refresh(action)
        assert action.status is RecoveryStatus.VERIFYING
        assert action.verified_at is None
        assert action.last_error is not None
        assert "partial_payment" in action.last_error
        holds = _audits(db_session, action.id, "verification.amount_mismatch")
        assert len(holds) == 1
        assert holds[0].details["reason"] == "partial_payment"
        assert holds[0].details["amount_paid_paise"] == 40_000

    def test_paid_status_but_underpaid_amount_paid_not_recovered(
        self, client, sign, db_session, make_opportunity
    ):
        """Defense in depth: even if the event claims `status: paid`, an
        `amount_paid` below the link amount is a partial payment — hold."""
        action = _make_link_action(
            db_session, make_opportunity, gateway_request_id="gwr_sec_under"
        )
        body = _link_paid_body(
            "gwr_sec_under",
            amount=100_000,
            amount_paid=40_000,
            currency="INR",
            status="paid",
        )
        r = _post(client, sign, body, "evt_sec_under")
        assert r.status_code == 200
        db_session.refresh(action)
        assert action.status is RecoveryStatus.VERIFYING
        holds = _audits(db_session, action.id, "verification.amount_mismatch")
        assert len(holds) == 1
        assert holds[0].details["reason"] == "partial_payment"

    def test_missing_amount_fails_closed(
        self, client, sign, db_session, make_opportunity
    ):
        """A link-paid payload without a verifiable integer amount cannot
        prove the action's amount was paid — hold, never guess."""
        action = _make_link_action(
            db_session, make_opportunity, gateway_request_id="gwr_sec_noamt"
        )
        body = _link_paid_body(
            "gwr_sec_noamt", amount_paid=100_000, currency="INR"
        )
        r = _post(client, sign, body, "evt_sec_noamt")
        assert r.status_code == 200
        db_session.refresh(action)
        assert action.status is RecoveryStatus.VERIFYING
        assert action.verified_at is None
        holds = _audits(db_session, action.id, "verification.amount_mismatch")
        assert len(holds) == 1
        assert holds[0].details["reason"] == "amount_unverifiable"
        assert holds[0].details["actual_paise"] is None

    def test_hold_is_not_terminal_corrected_delivery_recovers(
        self, client, sign, db_session, make_opportunity
    ):
        """The hold must not be a dead end: a LATER event (new event id)
        carrying the correct amount recovers the held action and clears the
        error — e.g. the customer completing a partial link."""
        action = _make_link_action(
            db_session, make_opportunity, gateway_request_id="gwr_sec_late"
        )
        bad = _link_paid_body(
            "gwr_sec_late", amount=90_000, amount_paid=90_000, currency="INR"
        )
        assert _post(client, sign, bad, "evt_sec_late_bad").status_code == 200
        db_session.refresh(action)
        assert action.status is RecoveryStatus.VERIFYING
        assert action.last_error is not None

        good = _link_paid_body(
            "gwr_sec_late", amount=100_000, amount_paid=100_000, currency="INR"
        )
        r = _post(client, sign, good, "evt_sec_late_good")
        assert r.status_code == 200
        assert r.json()["processed"] is True
        db_session.refresh(action)
        assert action.status is RecoveryStatus.RECOVERED
        assert action.verified_at is not None
        assert action.last_error is None
        assert len(_audits(db_session, action.id, "verification.amount_mismatch")) == 1
        assert len(_audits(db_session, action.id, "verify_recovered")) == 1

    def test_failed_action_still_recovers_on_matching_link_paid(
        self, client, sign, db_session, make_opportunity
    ):
        """Late success wins (FAILED is not terminal): the amount gate must
        not break the documented out-of-order recovery path."""
        action = _make_link_action(
            db_session,
            make_opportunity,
            gateway_request_id="gwr_sec_failed",
            status=RecoveryStatus.FAILED,
        )
        body = _link_paid_body(
            "gwr_sec_failed", amount=100_000, amount_paid=100_000, currency="INR"
        )
        r = _post(client, sign, body, "evt_sec_failed")
        assert r.status_code == 200
        db_session.refresh(action)
        assert action.status is RecoveryStatus.RECOVERED
        assert action.verified_at is not None


class TestAckDetailTruncation:
    """The ack `detail` (and the stored `webhook_events.error`) must not
    reflect unbounded payload-derived text back to the caller."""

    def test_handler_error_detail_capped(self, db_session, monkeypatch):
        full_text = "payload-derived:" + "A" * 5_000

        def _raising_handler(db, payload):
            raise RuntimeError(full_text)

        monkeypatch.setitem(EVENT_HANDLERS, "evil.event", _raising_handler)
        processed, detail = dispatch_event(db_session, "evil.event", {})
        assert processed is False
        assert detail is not None
        assert detail.startswith("handler error: RuntimeError: payload-derived:")
        assert detail.endswith(_TRUNC_SUFFIX)
        assert len(detail) <= _DETAIL_MAX_CHARS + len(_TRUNC_SUFFIX)
        assert full_text not in detail

    def test_handler_note_echo_capped_in_ack_and_stored_row(
        self, client, sign, db_session
    ):
        giant_id = "pay_" + "B" * 5_000
        body = json.dumps(
            {
                "entity": "event",
                "event": "payment.captured",
                "contains": ["payment"],
                "payload": {
                    "payment": {
                        "entity": {
                            "id": giant_id,
                            "entity": "payment",
                            "amount": 50000,
                            "currency": "INR",
                            "status": "captured",
                        }
                    }
                },
                "created_at": 1700000000,
            }
        ).encode()
        r = _post(client, sign, body, "evt_sec_giant_note")
        assert r.status_code == 200
        ack = r.json()
        assert ack["processed"] is False
        assert ack["detail"].startswith("unknown payment pay_")
        assert ack["detail"].endswith(_TRUNC_SUFFIX)
        assert len(ack["detail"]) <= _DETAIL_MAX_CHARS + len(_TRUNC_SUFFIX)
        assert giant_id not in ack["detail"]

        row = db_session.scalar(
            sa.select(models.WebhookEvent).where(
                models.WebhookEvent.gateway_event_id == "evt_sec_giant_note"
            )
        )
        assert row is not None
        assert len(row.error) <= _DETAIL_MAX_CHARS + len(_TRUNC_SUFFIX)
        assert giant_id not in row.error

    def test_short_detail_passes_through_unchanged(self, client, sign):
        body = json.dumps(
            {
                "entity": "event",
                "event": "payment.captured",
                "contains": ["payment"],
                "payload": {
                    "payment": {
                        "entity": {
                            "id": "pay_short",
                            "entity": "payment",
                            "amount": 50000,
                            "currency": "INR",
                            "status": "captured",
                        }
                    }
                },
                "created_at": 1700000000,
            }
        ).encode()
        r = _post(client, sign, body, "evt_sec_short_note")
        assert r.status_code == 200
        assert r.json()["detail"] == (
            "unknown payment pay_short; stored for reconciliation"
        )

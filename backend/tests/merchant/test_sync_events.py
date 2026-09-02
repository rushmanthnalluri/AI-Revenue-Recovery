"""Sync-derived payment events: the REST sync appends honest observation rows
to the payment_events stream — the real merchant environment's detection
signal alongside webhooks.

Covered contract (app/services/merchant/service.py module docstring):
- first seen in `created` -> one `payment.created` event at the GATEWAY
  timestamp (source semantics);
- first seen already terminal -> ONE observation event at OBSERVATION time
  (`ingested_at`), payload marked `derived_from: "sync"` — never backdated;
- status flip between syncs -> one transition event at observation time;
- a re-sync with no status change emits ZERO rows (idempotent);
- events copy the payment's provenance, so environment isolation holds and
  detection/dashboard consume the derived stream like webhook-derived rows.
"""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session

import app.models as models
from app.models.base import source_types_for_environment
from app.schemas.detection import DetectionRunRequest
from app.services.detection import run_detection
from app.services.detection.series import latest_event_anchor, load_outcomes
from app.services.merchant import SyncService
from tests.merchant.conftest import (
    FIXTURE_TS,
    FakeRazorpayAPI,
    payment_payload,
)

FIXTURE_DT = datetime.fromtimestamp(FIXTURE_TS, tz=timezone.utc)
REAL_TEST_TYPES = source_types_for_environment("real_test")
RESEARCH_TYPES = source_types_for_environment("research")


def _run_sync(sync_service: SyncService, db: Session) -> None:
    run = sync_service.run_sync(db, actor="test:sync")
    assert run.status == "completed"
    assert run.entity_counts["errors"] == []


def _payment(db: Session, external_id: str) -> models.Payment:
    payment = db.scalar(
        sa.select(models.Payment).where(models.Payment.external_id == external_id)
    )
    assert payment is not None
    return payment


def _events(db: Session, payment: models.Payment) -> list[models.PaymentEvent]:
    return list(
        db.scalars(
            sa.select(models.PaymentEvent)
            .where(models.PaymentEvent.payment_id == payment.id)
            .order_by(models.PaymentEvent.occurred_at, models.PaymentEvent.id)
        )
    )


def _event_count(db: Session) -> int:
    return int(db.scalar(sa.select(sa.func.count()).select_from(models.PaymentEvent)) or 0)


# ---------------------------------------------------------------------------
# first-sight states
# ---------------------------------------------------------------------------


def test_first_seen_created_emits_gateway_stamped_created_event(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    fake_api.collections["payments"] = [
        payment_payload("pay_FixNew01", status="created", captured=False)
    ]
    _run_sync(sync_service, db_session)

    payment = _payment(db_session, "pay_FixNew01")
    events = _events(db_session, payment)
    assert len(events) == 1
    (event,) = events
    assert event.event_type == "payment.created"
    assert event.from_status is None
    assert event.to_status == "created"
    assert event.source == "sync"
    # Source timestamp semantics: creation really happened at gateway time.
    assert event.occurred_at == FIXTURE_DT
    assert event.occurred_at == payment.gateway_created_at
    # Provenance copied from the payment (real_test environment isolation).
    assert event.source_type == "razorpay_test"
    assert event.source_system == "razorpay"
    assert event.external_id == "pay_FixNew01"


def test_first_seen_terminal_emits_exactly_one_observation_event(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    fake_api.collections["payments"] = [payment_payload("pay_FixCap01")]
    _run_sync(sync_service, db_session)

    payment = _payment(db_session, "pay_FixCap01")
    events = _events(db_session, payment)
    assert len(events) == 1
    (event,) = events
    assert event.event_type == "payment.captured"
    assert event.from_status is None
    assert event.to_status == "captured"
    assert event.source == "sync"
    # OBSERVATION time (ingested_at) — never backdated to the gateway
    # created_at: sync only knows what it saw when it saw it.
    assert event.occurred_at == payment.ingested_at
    assert event.occurred_at > FIXTURE_DT
    assert event.payload["derived_from"] == "sync"
    assert event.payload["observed_status"] == "captured"


def test_first_seen_failed_observation_keeps_failure_telemetry(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    fake_api.collections["payments"] = [
        payment_payload(
            "pay_FixFail1",
            status="failed",
            captured=False,
            error_code="BAD_REQUEST_ERROR",
            error_description="insufficient_fund",
            error_source="bank",
            error_reason="insufficient_fund",
        )
    ]
    _run_sync(sync_service, db_session)

    payment = _payment(db_session, "pay_FixFail1")
    events = _events(db_session, payment)
    assert len(events) == 1
    (event,) = events
    assert event.event_type == "payment.failed"
    assert event.to_status == "failed"
    assert event.occurred_at == payment.ingested_at
    assert event.payload["derived_from"] == "sync"
    assert event.payload["observed_status"] == "failed"
    # The raw entity stays top-level in the payload, exactly like the
    # webhook-derived shape — the insufficient_fund_share metric reads it.
    assert event.payload["error_reason"] == "insufficient_fund"
    assert event.payload["error_code"] == "BAD_REQUEST_ERROR"


# ---------------------------------------------------------------------------
# transitions + idempotency
# ---------------------------------------------------------------------------


def test_status_flip_between_syncs_emits_one_transition_event(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    fake_api.collections["payments"] = [
        payment_payload("pay_FixFlip1", status="created", captured=False)
    ]
    _run_sync(sync_service, db_session)

    # The gateway state flips; the next sync observes the transition.
    fake_api.collections["payments"] = [payment_payload("pay_FixFlip1")]
    _run_sync(sync_service, db_session)

    payment = _payment(db_session, "pay_FixFlip1")
    assert payment.status == "captured"
    events = _events(db_session, payment)
    assert len(events) == 2
    created_event, transition = events
    assert created_event.event_type == "payment.created"
    assert transition.event_type == "payment.captured"
    assert transition.from_status == "created"
    assert transition.to_status == "captured"
    assert transition.source == "sync"
    # Observation time, not the (older) gateway creation timestamp.
    assert transition.occurred_at > FIXTURE_DT
    assert transition.payload["derived_from"] == "sync"


def test_resync_without_changes_emits_zero_events(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    fake_api.collections["payments"] = [
        payment_payload("pay_FixNew01", status="created", captured=False),
        payment_payload("pay_FixCap01"),
        payment_payload("pay_FixFail1", status="failed", captured=False),
    ]
    _run_sync(sync_service, db_session)
    assert _event_count(db_session) == 3  # one first-sight event each

    _run_sync(sync_service, db_session)
    assert _event_count(db_session) == 3  # idempotent: zero new rows
    _run_sync(sync_service, db_session)
    assert _event_count(db_session) == 3


def test_sync_events_stay_in_the_real_test_environment(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    fake_api.collections["payments"] = [payment_payload("pay_FixCap01")]
    _run_sync(sync_service, db_session)

    provenance = db_session.execute(
        sa.select(
            models.PaymentEvent.source_type,
            models.PaymentEvent.source_system,
            models.PaymentEvent.source,
        )
    ).all()
    assert provenance == [("razorpay_test", "razorpay", "sync")]
    # The research (simulator) environment is untouched and stays blind only
    # to its own (absent) stream — the anchor split proves isolation.
    assert latest_event_anchor(db_session, source_types=REAL_TEST_TYPES) is not None
    assert latest_event_anchor(db_session, source_types=RESEARCH_TYPES) is None


# ---------------------------------------------------------------------------
# consumption: detection + dashboard see the sync-derived stream
# ---------------------------------------------------------------------------


def test_detection_and_dashboard_consume_sync_derived_events(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI, client
) -> None:
    """Acceptance proof: sync fixture payments -> derived events -> detection
    input window -> dashboard payments_observed > 0."""
    fake_api.collections["payments"] = [
        payment_payload("pay_FixCap01"),
        payment_payload("pay_FixCap02"),
        payment_payload(
            "pay_FixFail1",
            status="failed",
            captured=False,
            error_reason="insufficient_fund",
        ),
    ]
    _run_sync(sync_service, db_session)

    # The detector's window anchors on the derived terminal events...
    anchor = latest_event_anchor(db_session, source_types=REAL_TEST_TYPES)
    assert anchor is not None
    # ...and resolves one terminal outcome per synced payment — this is
    # exactly the dashboard's payments_observed computation.
    outcomes = load_outcomes(
        db_session, anchor - timedelta(hours=1), anchor, source_types=REAL_TEST_TYPES
    )
    assert len(outcomes) == 3
    assert sum(1 for o in outcomes if o.success) == 2

    result = run_detection(
        db_session, DetectionRunRequest(environment="real_test", dry_run=True)
    )
    assert result.status == "completed"
    assert result.detail is not None and "outcomes=3" in result.detail

    response = client.get(
        "/api/v1/dashboard/summary",
        params={"environment": "real_test"},
    )
    assert response.status_code == 200
    assert response.json()["payments_observed"] == 3

    # Research stays blind: nothing synced there, nothing detected there.
    research = run_detection(
        db_session, DetectionRunRequest(environment="research", dry_run=True)
    )
    assert research.detail == "no terminal payment events in scope; nothing to detect"


def test_webhook_recorded_transition_is_not_duplicated_by_sync(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    """Cross-source dedupe: the authoritative webhook event for a transition
    lands first; the next sync first-sees the same payment in the same state
    and must NOT append a second event for it."""
    fake_api.collections["payments"] = [payment_payload("pay_WhFirst1", status="captured")]
    # The webhook-derived event already exists for this payment+state.
    from app.db import utcnow

    merchant = models.Merchant(name="wh-first merchant", source_type="razorpay_test")
    db_session.add(merchant)
    db_session.flush()
    payment = models.Payment(
        merchant_id=merchant.id,
        amount_paise=50_000,
        status="captured",
        source_type="razorpay_test",
        source_system="razorpay",
        external_id="pay_WhFirst1",
        gateway_payment_id="pay_WhFirst1",
    )
    db_session.add(payment)
    db_session.flush()
    db_session.add(
        models.PaymentEvent(
            payment_id=payment.id,
            event_type="payment.captured",
            from_status="authorized",
            to_status="captured",
            source="webhook",
            payload={},
            occurred_at=utcnow(),
            source_type="razorpay_test",
            source_system="razorpay",
            external_id="pay_WhFirst1",
        )
    )
    db_session.commit()

    _run_sync(sync_service, db_session)

    events = list(
        db_session.scalars(
            sa.select(models.PaymentEvent).where(
                models.PaymentEvent.payment_id == payment.id,
                models.PaymentEvent.to_status == "captured",
            )
        )
    )
    assert len(events) == 1  # the webhook row only — sync observed nothing new
    assert events[0].source == "webhook"

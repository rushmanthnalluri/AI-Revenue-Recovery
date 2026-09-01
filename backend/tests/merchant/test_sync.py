"""Full-sync tests: pagination, normalization, provenance, idempotency,
quarantine, sync_runs/connection_state bookkeeping, enable/disable, and
secret hygiene. All gateway traffic is fixture-backed (httpx.MockTransport).
"""

import httpx
import pytest
import sqlalchemy as sa
from datetime import datetime, timezone
from sqlalchemy.orm import Session

import app.models as models
from app.db import utcnow
from app.ports import ActionType, RecoveryStatus
from app.services.merchant import (
    SyncDisabledError,
    SyncNotConfiguredError,
    SyncService,
)
from tests.merchant.conftest import (
    API_KEY_HEADERS,
    FIXTURE_TS,
    KEY_ID,
    KEY_SECRET,
    FakeRazorpayAPI,
    error_response,
    order_payload,
    payment_link_payload,
    payment_payload,
    subscription_payload,
)

FIXTURE_DT = datetime.fromtimestamp(FIXTURE_TS, tz=timezone.utc)


def _count(db: Session, model) -> int:
    return int(db.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


@pytest.fixture()
def stocked_api(fake_api: FakeRazorpayAPI) -> FakeRazorpayAPI:
    """A small fixture catalog: 2 orders, 3 payments (one failed, one
    pointing at an unknown order), 1 subscription."""
    fake_api.collections["orders"] = [
        order_payload("order_FixOrd01", status="paid", amount_paid=50_000),
        order_payload("order_FixOrd02", receipt="act_fixture2"),
    ]
    fake_api.collections["payments"] = [
        payment_payload("pay_FixPay01"),
        payment_payload(
            "pay_FixFail1",
            status="failed",
            method="card",
            captured=False,
            error_code="BAD_REQUEST_ERROR",
            error_description="insufficient_fund",
            error_source="bank",
            error_reason="insufficient_fund",
        ),
        payment_payload("pay_FixPay03", order_id=None),
    ]
    fake_api.collections["subscriptions"] = [subscription_payload("sub_FixSub01")]
    return fake_api


# ---------------------------------------------------------------------------
# pagination + normalization + provenance
# ---------------------------------------------------------------------------


def test_sync_paginates_normalizes_and_stamps_provenance(
    db_session: Session, sync_service: SyncService, stocked_api: FakeRazorpayAPI
) -> None:
    run = sync_service.run_sync(db_session, actor="test:sync", request_id="req_fixture")

    assert run.status == "completed"
    assert run.finished_at is not None
    assert run.id.startswith("sr_")
    assert run.actor == "test:sync"
    assert run.request_id == "req_fixture"
    assert run.entity_counts["orders"] == {"created": 2, "updated": 0}
    assert run.entity_counts["payments"] == {"created": 3, "updated": 0}
    assert run.entity_counts["subscriptions"] == {"created": 1, "updated": 0}
    assert run.entity_counts["errors"] == []

    # The auth canary probes payments?count=1 first (no window params).
    probe_requests = [p for c, p in stocked_api.requests if c == "payments" and "from" not in p]
    assert [p["count"] for p in probe_requests] == ["1"]
    # Pagination: page_size=2 over 3 payments -> two count/skip requests with
    # the documented window parameters.
    payment_requests = [p for c, p in stocked_api.requests if c == "payments" and "from" in p]
    assert [(p["count"], p["skip"]) for p in payment_requests] == [("2", "0"), ("2", "2")]
    assert all("to" in p for p in payment_requests)

    # Provenance + normalization on a captured payment.
    payment = db_session.scalar(
        sa.select(models.Payment).where(models.Payment.external_id == "pay_FixPay01")
    )
    assert payment is not None
    assert payment.source_type == "razorpay_test"
    assert payment.source_system == "razorpay"
    assert payment.external_id == "pay_FixPay01"
    assert payment.gateway_payment_id == "pay_FixPay01"
    assert payment.ingested_at is not None
    assert payment.created_at == FIXTURE_DT  # gateway timestamp, not ingest time
    assert payment.gateway_created_at == FIXTURE_DT
    assert payment.amount_paise == 50_000
    assert payment.status == "captured"
    assert payment.method == "upi"
    assert payment.captured is True
    assert payment.meta["razorpay"]["id"] == "pay_FixPay01"  # raw snapshot kept

    # The failed payment carries the error quintet (code/description/source as
    # columns; step/reason in meta).
    failed = db_session.scalar(
        sa.select(models.Payment).where(models.Payment.external_id == "pay_FixFail1")
    )
    assert failed.status == "failed"
    assert failed.error_code == "BAD_REQUEST_ERROR"
    assert failed.error_description == "insufficient_fund"
    assert failed.error_source == "bank"
    assert failed.meta["error_reason"] == "insufficient_fund"

    # Order linkage: known gateway order -> local FK; unknown -> NULL.
    order = db_session.scalar(
        sa.select(models.Order).where(models.Order.external_id == "order_FixOrd01")
    )
    assert order is not None and order.source_type == "razorpay_test"
    assert payment.order_id == order.id
    orphan = db_session.scalar(
        sa.select(models.Payment).where(models.Payment.external_id == "pay_FixPay03")
    )
    assert orphan.order_id is None

    # Subscription: amount_unknown honesty + provenance.
    sub = db_session.scalar(
        sa.select(models.Subscription).where(
            models.Subscription.external_id == "sub_FixSub01"
        )
    )
    assert sub.source_type == "razorpay_test"
    assert sub.gateway_subscription_id == "sub_FixSub01"
    assert sub.status == "active"
    assert sub.plan_id == "plan_FixPlan01"
    assert sub.meta["amount_unknown"] is True

    # The merchant anchor was created with real provenance.
    merchant = db_session.scalar(
        sa.select(models.Merchant).where(models.Merchant.id == payment.merchant_id)
    )
    assert merchant.source_type == "razorpay_test"
    assert merchant.source_system == "razorpay"


def test_sync_window_parameters(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    sync_service.run_sync(db_session, actor="test:sync", window_days=7)
    _, params = next(r for r in fake_api.requests if r[0] == "orders")
    assert int(params["to"]) - int(params["from"]) == 7 * 86_400


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_resync_is_idempotent_zero_duplicates(
    db_session: Session, sync_service: SyncService, stocked_api: FakeRazorpayAPI
) -> None:
    sync_service.run_sync(db_session, actor="test:sync")
    before = {
        m: _count(db_session, m)
        for m in (models.Merchant, models.Order, models.Payment, models.Subscription)
    }

    second = sync_service.run_sync(db_session, actor="test:sync")

    after = {
        m: _count(db_session, m)
        for m in (models.Merchant, models.Order, models.Payment, models.Subscription)
    }
    assert after == before  # ZERO new rows on re-sync
    assert second.entity_counts["orders"] == {"created": 0, "updated": 2}
    assert second.entity_counts["payments"] == {"created": 0, "updated": 3}
    assert second.entity_counts["subscriptions"] == {"created": 0, "updated": 1}

    # (source_type, external_id) stays unique per row.
    dupes = db_session.execute(
        sa.select(models.Payment.source_type, models.Payment.external_id, sa.func.count())
        .group_by(models.Payment.source_type, models.Payment.external_id)
        .having(sa.func.count() > 1)
    ).all()
    assert dupes == []


def test_resync_updates_changed_fields(
    db_session: Session, sync_service: SyncService, stocked_api: FakeRazorpayAPI
) -> None:
    sync_service.run_sync(db_session, actor="test:sync")
    # The upstream entity changes (created -> captured); re-sync updates in
    # place and never regresses created_at/ingested_at.
    stocked_api.collections["payments"][0] = payment_payload(
        "pay_FixPay01", status="authorized", captured=False
    )
    run = sync_service.run_sync(db_session, actor="test:sync")
    assert run.status == "completed"
    payment = db_session.scalar(
        sa.select(models.Payment).where(models.Payment.external_id == "pay_FixPay01")
    )
    assert payment.status == "authorized"
    assert payment.captured is False
    assert payment.created_at == FIXTURE_DT


# ---------------------------------------------------------------------------
# validation / quarantine
# ---------------------------------------------------------------------------


def test_bad_entities_are_quarantined_not_fatal(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    fake_api.collections["payments"] = [
        payment_payload("pay_Good01"),
        payment_payload("pay_NoAmount", amount="not-an-int"),  # bad amount
        {k: v for k, v in payment_payload().items() if k != "id"},  # missing id
        payment_payload("pay_BadStatus", status="mysterious"),  # undocumented enum
    ]
    run = sync_service.run_sync(db_session, actor="test:sync")

    assert run.status == "completed"  # the run never crashes on bad entities
    assert _count(db_session, models.Payment) == 1
    errors = run.entity_counts["errors"]
    assert len(errors) == 3
    assert {e["entity"] for e in errors} == {"payment"}
    assert {e["id"] for e in errors} == {"pay_NoAmount", "pay_BadStatus", None}
    assert all(e["reason"] for e in errors)


def test_malformed_collection_envelope_fails_the_run(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    fake_api.failures["orders"] = httpx.Response(200, json={"entity": "weird"})
    run = sync_service.run_sync(db_session, actor="test:sync")
    assert run.status == "failed"
    assert "GatewayResponseError" in (run.error or "")
    state = db_session.get(models.ConnectionState, "merchant")
    assert state.last_sync_status == "failed"


def test_gateway_auth_failure_marks_run_failed(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    # Bad keys: the probe canary (GET /v1/payments?count=1) is refused, so
    # nothing can sync — the run fails before any catalog pull.
    fake_api.failures["payments"] = error_response(401, "Your api key/secret is invalid")
    run = sync_service.run_sync(db_session, actor="test:sync")
    assert run.status == "failed"
    assert run.error is not None and "GatewayAuthenticationError" in run.error
    state = db_session.get(models.ConnectionState, "merchant")
    assert state.last_sync_status == "failed"
    assert state.last_sync_at is not None


def test_endpoint_refusal_degrades_per_entity(
    db_session: Session, sync_service: SyncService, stocked_api: FakeRazorpayAPI
) -> None:
    """Regression for the 2026-09-01 production incident: Razorpay answers 401
    on GET /v1/subscriptions when the Subscriptions product is not enabled on
    the account, while every other endpoint authenticates fine. The run must
    complete with the rest of the catalog ingested and the skip recorded."""
    stocked_api.failures["subscriptions"] = error_response(401, "unauthorized")
    run = sync_service.run_sync(db_session, actor="test:sync")

    assert run.status == "completed"
    assert run.entity_counts["orders"] == {"created": 2, "updated": 0}
    assert run.entity_counts["payments"] == {"created": 3, "updated": 0}
    assert run.entity_counts["subscriptions"] == {"created": 0, "updated": 0}
    errors = run.entity_counts["errors"]
    assert len(errors) == 1
    assert errors[0]["entity"] == "subscription"
    assert "endpoint skipped" in errors[0]["reason"]
    assert "GatewayAuthenticationError" in errors[0]["reason"]
    assert "enabled" in errors[0]["reason"]
    # The rest of the catalog really landed.
    assert _count(db_session, models.Order) == 2
    assert _count(db_session, models.Payment) == 3


def test_partial_endpoint_refusals_complete_with_skips(
    db_session: Session, sync_service: SyncService, stocked_api: FakeRazorpayAPI
) -> None:
    """Multiple endpoints 4xx-refused (probe target payments excepted, so the
    canary passes): the run still completes with every skip recorded."""
    for collection in ("orders", "subscriptions"):
        stocked_api.failures[collection] = error_response(403, "forbidden")
    run = sync_service.run_sync(db_session, actor="test:sync")
    # orders + subscriptions refused; payments + payment_links pulled clean ->
    # the run completes with two skips recorded.
    assert run.status == "completed"
    assert {e["entity"] for e in run.entity_counts["errors"]} == {"order", "subscription"}


# ---------------------------------------------------------------------------
# payment links by known reference_ids
# ---------------------------------------------------------------------------


def _make_link_action(
    db_session: Session, *, gateway_request_id: str, environment: str
) -> models.RecoveryAction:
    incident = models.Incident(
        title="fixture incident",
        metric="payment_success_rate",
        detected_at=utcnow(),
        environment=environment,
    )
    db_session.add(incident)
    db_session.flush()
    opp = models.RecoveryOpportunity(
        incident_id=incident.id,
        opportunity_type="failed_payment_retry",
        amount_paise=50_000,
        environment=environment,
    )
    db_session.add(opp)
    db_session.flush()
    action = models.RecoveryAction(
        opportunity_id=opp.id,
        action_type=ActionType.CREATE_PAYMENT_LINK,
        status=RecoveryStatus.EXECUTING,
        amount_paise=50_000,
        gateway_request_id=gateway_request_id,
        proposed_at=utcnow(),
        environment=environment,
    )
    db_session.add(action)
    db_session.commit()
    return action


def test_payment_links_fetched_by_known_reference_ids(
    db_session: Session, sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    _make_link_action(db_session, gateway_request_id="act_reallink1", environment="real_test")
    # A research-environment action must NOT be reconciled against the real API.
    _make_link_action(db_session, gateway_request_id="act_simlink9", environment="research")
    fake_api.payment_links["act_reallink1"] = [
        payment_link_payload(
            "plink_FixPlink1",
            reference_id="act_reallink1",
            payments=[payment_payload("pay_FromLink1", order_id=None)],
        )
    ]

    run = sync_service.run_sync(db_session, actor="test:sync")

    assert run.status == "completed"
    assert run.entity_counts["payment_links"] == {"fetched": 1}
    # Only the real_test reference_id was queried.
    link_requests = [p for c, p in fake_api.requests if c == "payment_links"]
    assert link_requests == [{"reference_id": "act_reallink1"}]
    # The link's post-capture payment was ingested as a real payment.
    payment = db_session.scalar(
        sa.select(models.Payment).where(models.Payment.external_id == "pay_FromLink1")
    )
    assert payment is not None
    assert payment.source_type == "razorpay_test"


# ---------------------------------------------------------------------------
# guards: not configured / disabled
# ---------------------------------------------------------------------------


def test_run_sync_refuses_when_not_configured(db_session: Session) -> None:
    service = SyncService(key_id="", key_secret="")
    with pytest.raises(SyncNotConfiguredError):
        service.run_sync(db_session, actor="test:sync")
    assert _count(db_session, models.SyncRun) == 0  # refused before any writes


def test_run_sync_refuses_when_disabled(
    db_session: Session, sync_service: SyncService
) -> None:
    sync_service.set_sync_enabled(db_session, False)
    with pytest.raises(SyncDisabledError):
        sync_service.run_sync(db_session, actor="test:sync")
    assert _count(db_session, models.SyncRun) == 0


def test_sync_runs_and_connection_state_written(
    db_session: Session, sync_service: SyncService, stocked_api: FakeRazorpayAPI
) -> None:
    run = sync_service.run_sync(db_session, actor="test:sync")
    state = db_session.get(models.ConnectionState, "merchant")
    assert state is not None
    assert state.sync_enabled is True
    assert state.last_sync_at == run.finished_at
    assert state.last_sync_status == "completed"
    stored = db_session.get(models.SyncRun, run.id)
    assert stored is not None and stored.entity_counts["payments"]["created"] == 3


# ---------------------------------------------------------------------------
# HTTP API surface
# ---------------------------------------------------------------------------


def test_connection_endpoint_shape(client, sync_service, stocked_api) -> None:
    resp = client.get("/api/v1/merchant/connection")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "configured",
        "connected",
        "environment",
        "key_id_masked",
        "webhook_configured",
        "sync_enabled",
        "last_sync_at",
        "last_webhook_at",
        "last_sync_status",
        "connection_error",
    }
    assert body["configured"] is True
    assert body["connected"] is True
    assert body["environment"] == "test"
    assert body["key_id_masked"] == "rzp_test_••••ID01"
    assert body["connection_error"] is None
    assert body["sync_enabled"] is True
    assert body["last_sync_at"] is None  # never synced

    client.post("/api/v1/merchant/sync", headers=API_KEY_HEADERS)
    body = client.get("/api/v1/merchant/connection").json()
    assert body["last_sync_status"] == "completed"
    assert body["last_sync_at"] is not None


def test_sync_endpoint_returns_run_summary(client, stocked_api) -> None:
    resp = client.post("/api/v1/merchant/sync", headers=API_KEY_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"].startswith("sr_")
    assert body["status"] == "completed"
    assert body["actor"] == "api:merchant_sync"
    assert body["entity_counts"]["payments"] == {"created": 3, "updated": 0}
    assert body["entity_counts"]["errors"] == []
    assert body["error"] is None


def test_sync_endpoint_requires_api_key(client, stocked_api) -> None:
    resp = client.post("/api/v1/merchant/sync")
    assert resp.status_code == 401


def test_sync_endpoint_409_when_disabled(client, db_session, sync_service) -> None:
    resp = client.post("/api/v1/merchant/sync/disable", headers=API_KEY_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["sync_enabled"] is False

    resp = client.post("/api/v1/merchant/sync", headers=API_KEY_HEADERS)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"].startswith("sync_disabled")

    resp = client.post("/api/v1/merchant/sync/enable", headers=API_KEY_HEADERS)
    assert resp.json()["sync_enabled"] is True
    resp = client.post("/api/v1/merchant/sync", headers=API_KEY_HEADERS)
    assert resp.status_code == 200

    # Disconnect/Reconnect toggles are audited.
    actions = db_session.scalars(
        sa.select(models.AuditLog.action).where(
            models.AuditLog.entity_type == "connection_state"
        )
    ).all()
    assert "merchant.sync_disable" in actions
    assert "merchant.sync_enable" in actions


def test_sync_toggle_audit_row_is_real_test_tagged(client, db_session) -> None:
    """The connection singleton is the REAL Razorpay Test Mode merchant — the
    toggle audit row must be queryable in the real_test environment, not
    default to research."""
    resp = client.post("/api/v1/merchant/sync/disable", headers=API_KEY_HEADERS)
    assert resp.status_code == 200

    row = db_session.scalar(
        sa.select(models.AuditLog).where(
            models.AuditLog.action == "merchant.sync_disable"
        )
    )
    assert row is not None
    assert row.environment == "real_test"


def test_sync_endpoint_409_when_not_configured(db_session) -> None:
    from fastapi.testclient import TestClient

    from app.api.v1.merchant import get_sync_service
    from app.db import get_db
    from app.main import create_app

    app = create_app()

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_sync_service] = lambda: SyncService(
        key_id="", key_secret=""
    )
    with TestClient(app) as c:
        resp = c.post("/api/v1/merchant/sync", headers=API_KEY_HEADERS)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"].startswith("razorpay_not_configured")


# ---------------------------------------------------------------------------
# secret hygiene
# ---------------------------------------------------------------------------


def test_secret_never_appears_in_responses_or_logs(
    client, sync_service, fake_api: FakeRazorpayAPI, caplog
) -> None:
    """Even on failure paths, responses and logs carry the masked key id at
    most — never the full key id, never the secret."""
    import logging

    fake_api.failures["payments"] = error_response(401, "Your api key/secret is invalid")
    fake_api.failures["orders"] = error_response(401, "Your api key/secret is invalid")

    with caplog.at_level(logging.DEBUG):
        conn = client.get("/api/v1/merchant/connection")
        sync = client.post("/api/v1/merchant/sync", headers=API_KEY_HEADERS)

    assert conn.json()["connected"] is False
    assert conn.json()["connection_error"] == "authentication_failed"
    assert sync.json()["status"] == "failed"
    for text in (conn.text, sync.text, caplog.text):
        assert KEY_SECRET not in text
        assert KEY_ID not in text  # only the masked form may appear
    assert "rzp_test_••••ID01" in conn.text

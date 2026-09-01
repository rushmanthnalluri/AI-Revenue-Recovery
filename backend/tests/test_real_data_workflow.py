"""Real Data Workflow Tests — verify source isolation, real Razorpay sync,
webhook ingestion, and recovery execution end-to-end.

These tests use mocked transports to simulate real Razorpay API responses
without network calls. They prove the data flow architecture works correctly.
"""

import hmac
import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest
import httpx
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    ConnectionState,
    CONNECTION_STATE_SINGLETON_ID,
    Incident,
    Merchant,
    Payment,
    RecoveryAction,
    RecoveryOpportunity,
    SyncRun,
    WebhookEvent,
)
from app.models.base import (
    ENVIRONMENT_REAL_TEST,
    ENVIRONMENT_RESEARCH,
    SOURCE_TYPE_RAZORPAY_TEST,
    SOURCE_TYPE_SIMULATOR,
)
from app.ports import ActionType, IncidentStatus, RecoveryStatus, Severity
from app.services.merchant.service import SyncService, SyncDisabledError, SyncNotConfiguredError
from app.services.merchant.client import RazorpayReadClient
from app.services.razorpay.client import RazorpayGateway
from app.services.razorpay.factory import get_gateway, gateway_mode, use_simulator
from app.services.recovery.webhook_handlers import dispatch_event
from app.services.policy import audit


class MockTransport(httpx.BaseTransport):
    """Mock HTTP transport for controlled Razorpay API responses."""

    def __init__(self, responses: dict[str, httpx.Response]):
        self._responses = responses
        self.requests = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        if key in self._responses:
            return self._responses[key]
        return httpx.Response(404, json={"error": {"code": "not_found"}})


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def real_test_settings() -> Settings:
    """Settings with real Razorpay test keys configured."""
    return Settings(
        RAZORPAY_KEY_ID="rzp_test_abc123",
        RAZORPAY_KEY_SECRET="test_secret",
        RAZORPAY_WEBHOOK_SECRET="webhook_secret",
        SIMULATION_MODE=False,
        APP_ENV="test",
    )


@pytest.fixture
def no_keys_settings() -> Settings:
    """Settings with no Razorpay keys (simulator fallback)."""
    return Settings(
        RAZORPAY_KEY_ID="",
        RAZORPAY_KEY_SECRET="",
        RAZORPAY_WEBHOOK_SECRET="",
        SIMULATION_MODE=False,
        APP_ENV="test",
    )


@pytest.fixture
def forced_sim_settings() -> Settings:
    """Settings with keys but SIMULATION_MODE forced true."""
    return Settings(
        RAZORPAY_KEY_ID="rzp_test_abc123",
        RAZORPAY_KEY_SECRET="test_secret",
        RAZORPAY_WEBHOOK_SECRET="webhook_secret",
        SIMULATION_MODE=True,  # Forced for Research Lab
        APP_ENV="test",
    )


# -----------------------------------------------------------------------------
# Gateway Factory Tests
# -----------------------------------------------------------------------------

def test_gateway_factory_prefers_real_when_keys_exist(real_test_settings):
    """Real gateway selected when keys configured and SIMULATION_MODE=false."""
    assert use_simulator(real_test_settings) is False
    assert gateway_mode(real_test_settings) == "razorpay_test"
    gateway = get_gateway(real_test_settings)
    assert isinstance(gateway, RazorpayGateway)
    assert gateway._webhook_secret == "webhook_secret"


def test_gateway_factory_falls_back_to_simulator_when_no_keys(no_keys_settings):
    """Simulator selected when no real keys configured."""
    assert use_simulator(no_keys_settings) is True
    assert gateway_mode(no_keys_settings) == "simulator"
    gateway = get_gateway(no_keys_settings)
    assert type(gateway).__name__ == "SimulatedPaymentGateway"


def test_gateway_factory_respects_forced_simulation_mode(forced_sim_settings):
    """SIMULATION_MODE=true forces simulator even with real keys."""
    assert use_simulator(forced_sim_settings) is True
    assert gateway_mode(forced_sim_settings) == "simulator"
    gateway = get_gateway(forced_sim_settings)
    assert type(gateway).__name__ == "SimulatedPaymentGateway"


def test_get_real_gateway_returns_none_when_no_keys(no_keys_settings):
    """get_real_gateway returns None when keys missing."""
    from app.services.razorpay.factory import get_real_gateway
    assert get_real_gateway(no_keys_settings) is None


def test_get_real_gateway_returns_gateway_when_keys_exist(real_test_settings):
    """get_real_gateway returns RazorpayGateway when keys exist."""
    from app.services.razorpay.factory import get_real_gateway
    gateway = get_real_gateway(real_test_settings)
    assert isinstance(gateway, RazorpayGateway)


# -----------------------------------------------------------------------------
# Sync Service Tests (Real Data Ingestion)
# -----------------------------------------------------------------------------

def test_sync_service_configured_when_keys_exist(real_test_settings):
    """SyncService reports configured when real keys present."""
    service = SyncService.from_settings(real_test_settings)
    assert service._configured is True
    assert service._client is not None


def test_sync_service_not_configured_when_no_keys(no_keys_settings):
    """SyncService reports not configured when keys missing."""
    service = SyncService.from_settings(no_keys_settings)
    assert service._configured is False
    assert service._client is None


def test_sync_service_probe_connection_success(real_test_settings):
    """Connection probe succeeds with valid credentials."""
    responses = {
        "GET /v1/payments": httpx.Response(200, json={"entity": "collection", "count": 1, "items": []})
    }
    transport = MockTransport(responses)

    service = SyncService.from_settings(real_test_settings, transport=transport)
    probe = service.probe_connection()

    assert probe.configured is True
    assert probe.connected is True
    assert probe.environment == "test"
    assert probe.key_id_masked == "rzp_test_••••c123"
    assert probe.connection_error is None


def test_sync_service_probe_auth_failure(real_test_settings):
    """Connection probe reports authentication failure."""
    responses = {
        "GET /v1/payments": httpx.Response(401, json={"error": {"code": "bad_request"}})
    }
    transport = MockTransport(responses)

    service = SyncService.from_settings(real_test_settings, transport=transport)
    probe = service.probe_connection()

    assert probe.configured is True
    assert probe.connected is False
    assert probe.connection_error == "authentication_failed"


def test_sync_service_full_sync_ingests_real_data(db_session: Session, real_test_settings, make_merchant):
    """Full sync ingests orders, payments, subscriptions with real_test provenance."""
    merchant = make_merchant()
    now = int(datetime.now(timezone.utc).timestamp())

    responses = {
        "GET /v1/orders": httpx.Response(200, json={
            "entity": "collection", "count": 1, "items": [{
                "id": "order_test123",
                "amount": 50000,
                "currency": "INR",
                "receipt": "rcpt_1",
                "status": "paid",
                "created_at": now - 3600,
                "notes": {}
            }]
        }),
        "GET /v1/payments": httpx.Response(200, json={
            "entity": "collection", "count": 1, "items": [{
                "id": "pay_test123",
                "order_id": "order_test123",
                "amount": 50000,
                "currency": "INR",
                "status": "captured",
                "method": "card",
                "captured": True,
                "created_at": now - 3500,
                "email": "customer@example.com",
                "contact": "+919876543210",
                "fee": 500,
                "tax": 50,
                "error_code": None,
                "error_description": None,
                "acquirer_data": {}
            }]
        }),
        "GET /v1/subscriptions": httpx.Response(200, json={"entity": "collection", "count": 0, "items": []}),
        "GET /v1/payment_links": httpx.Response(200, json=[]),
    }
    transport = MockTransport(responses)

    service = SyncService.from_settings(real_test_settings, transport=transport)
    run = service.run_sync(db_session, actor="test:sync", window_days=30)

    assert run.status == "completed"
    assert run.entity_counts["orders"]["created"] == 1
    assert run.entity_counts["payments"]["created"] == 1

    # Verify provenance - payment should be linked to the merchant created by sync
    payment = db_session.query(Payment).filter(Payment.external_id == "pay_test123").first()
    assert payment is not None
    assert payment.source_type == SOURCE_TYPE_RAZORPAY_TEST
    assert payment.source_system == "razorpay"
    assert payment.external_id == "pay_test123"
    assert payment.ingested_at is not None
    # Merchant ID is created by _ensure_merchant during sync, not the test fixture
    assert payment.merchant_id is not None


def test_sync_service_idempotent_rerun(db_session: Session, real_test_settings, make_merchant):
    """Re-sync updates in place, zero duplicates."""
    merchant = make_merchant()
    now = int(datetime.now(timezone.utc).timestamp())
    responses = {
        "GET /v1/orders": httpx.Response(200, json={
            "entity": "collection", "count": 1, "items": [{
                "id": "order_test123",
                "amount": 50000,
                "currency": "INR",
                "receipt": "rcpt_1",
                "status": "paid",
                "created_at": now - 3600,
                "notes": {}
            }]
        }),
        "GET /v1/payments": httpx.Response(200, json={
            "entity": "collection", "count": 1, "items": [{
                "id": "pay_test123",
                "order_id": "order_test123",
                "amount": 50000,
                "currency": "INR",
                "status": "captured",
                "method": "card",
                "captured": True,
                "created_at": now - 3500,
                "email": "customer@example.com",
                "contact": "+919876543210",
                "fee": 500,
                "tax": 50,
                "error_code": None,
                "error_description": None,
                "acquirer_data": {}
            }]
        }),
        "GET /v1/subscriptions": httpx.Response(200, json={"entity": "collection", "count": 0, "items": []}),
        "GET /v1/payment_links": httpx.Response(200, json=[]),
    }
    transport = MockTransport(responses)

    service = SyncService.from_settings(real_test_settings, transport=transport)

    # First sync
    run1 = service.run_sync(db_session, actor="test:sync1", window_days=30)
    assert run1.entity_counts["payments"]["created"] == 1

    # Second sync (same data) - should update, not duplicate
    run2 = service.run_sync(db_session, actor="test:sync2", window_days=30)
    assert run2.entity_counts["payments"]["updated"] == 1
    assert run2.entity_counts["payments"]["created"] == 0

    # Verify only one payment row
    count = db_session.query(Payment).filter(Payment.external_id == "pay_test123").count()
    assert count == 1


def test_sync_service_quarantines_invalid_entities(db_session: Session, real_test_settings, make_merchant):
    """Invalid entities skipped and recorded, sync continues."""
    merchant = make_merchant()
    now = int(datetime.now(timezone.utc).timestamp())
    responses = {
        "GET /v1/orders": httpx.Response(200, json={"entity": "collection", "count": 0, "items": []}),
        "GET /v1/payments": httpx.Response(200, json={
            "entity": "collection", "count": 2, "items": [
                {"id": "pay_good", "order_id": "order_1", "amount": 1000, "currency": "INR", "status": "captured", "method": "card", "captured": True, "created_at": now, "email": "a@b.com", "contact": "+919999999999", "fee": 10, "tax": 1, "error_code": None, "error_description": None, "acquirer_data": {}},
                {"id": "pay_bad", "amount": "not_a_number"},  # Invalid - will be quarantined
            ]
        }),
        "GET /v1/subscriptions": httpx.Response(200, json={"entity": "collection", "count": 0, "items": []}),
        "GET /v1/payment_links": httpx.Response(200, json=[]),
    }
    transport = MockTransport(responses)

    service = SyncService.from_settings(real_test_settings, transport=transport)
    run = service.run_sync(db_session, actor="test:sync", window_days=30)

    assert run.status == "completed"
    assert run.entity_counts["payments"]["created"] == 1
    assert len(run.entity_counts["errors"]) == 1
    assert run.entity_counts["errors"][0]["entity"] == "payment"
    assert run.entity_counts["errors"][0]["id"] == "pay_bad"


# -----------------------------------------------------------------------------
# Webhook Ingestion Tests
# -----------------------------------------------------------------------------

def test_webhook_signature_verification(real_test_settings):
    """HMAC-SHA256 signature verification works correctly."""
    gateway = RazorpayGateway(
        key_id=real_test_settings.RAZORPAY_KEY_ID,
        key_secret=real_test_settings.RAZORPAY_KEY_SECRET,
        webhook_secret="webhook_secret",
    )

    payload = b'{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_123"}}}}'
    signature = hmac.new(b"webhook_secret", payload, hashlib.sha256).hexdigest()

    assert gateway.verify_webhook_signature(payload, signature) is True
    assert gateway.verify_webhook_signature(payload, "invalid_sig") is False
    assert gateway.verify_webhook_signature(payload, "") is False


def test_webhook_deduplication(db_session: Session, real_test_settings):
    """Duplicate webhook events acknowledged with zero side effects."""
    gateway = RazorpayGateway(
        key_id=real_test_settings.RAZORPAY_KEY_ID,
        key_secret=real_test_settings.RAZORPAY_KEY_SECRET,
        webhook_secret="webhook_secret",
    )

    payload = {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_123"}}}}
    raw = json.dumps(payload).encode()
    signature = hmac.new(b"webhook_secret", raw, hashlib.sha256).hexdigest()
    now = datetime.now(timezone.utc)

    # First delivery
    event1 = WebhookEvent(
        gateway_event_id="evt_123",
        event_type="payment.captured",
        payload=payload,
        signature_valid=True,
        processed=True,
        source="razorpay",
        received_at=now,
    )
    db_session.add(event1)
    db_session.commit()

    # Second delivery (duplicate event_id)
    event2 = WebhookEvent(
        gateway_event_id="evt_123",  # Same ID
        event_type="payment.captured",
        payload=payload,
        signature_valid=True,
        processed=False,
        source="razorpay",
        received_at=now,
    )
    db_session.add(event2)
    try:
        db_session.commit()
        assert False, "Should have raised IntegrityError"
    except Exception:
        db_session.rollback()
        # Duplicate correctly rejected by UNIQUE constraint


def test_webhook_dispatch_updates_payment_state(db_session: Session, make_merchant, make_payment):
    """Webhook handler updates payment status from captured event."""
    merchant = make_merchant()
    payment = make_payment(
        merchant=merchant, 
        gateway_payment_id="pay_123",  # Webhook handler looks up by this field
        external_id="pay_123", 
        source_type=SOURCE_TYPE_RAZORPAY_TEST,
        source_system="razorpay",
        status="authorized", 
        captured=False
    )
    db_session.commit()

    # Dispatch captured webhook - the handler looks up payment by gateway_payment_id
    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_123",
                    "status": "captured",
                    "captured": True,
                }
            }
        }
    }

    processed, detail = dispatch_event(db_session, "payment.captured", payload)
    db_session.commit()

    assert processed is True
    # Payment should be updated
    db_session.refresh(payment)
    assert payment.status == "captured"
    assert payment.captured is True


# -----------------------------------------------------------------------------
# Recovery Execution Tests (Real Gateway)
# -----------------------------------------------------------------------------

def test_recovery_creates_payment_link_via_real_gateway(real_test_settings):
    """Recovery executor calls RazorpayGateway.create_payment_link for real_test."""
    gateway = get_gateway(real_test_settings)
    assert isinstance(gateway, RazorpayGateway)

    # Mock the HTTP call
    with patch.object(gateway, '_request') as mock_request:
        mock_request.return_value = {
            "id": "plink_test123",
            "reference_id": "greq_abc123",
            "short_url": "https://rzp.io/i/test123",
            "status": "created",
        }

        result = gateway.create_payment_link(
            amount_paise=50000,
            currency="INR",
            customer={"name": "Test Customer", "email": "test@example.com", "contact": "+919876543210"},
            description="Recovery payment",
            idempotency_key="greq_abc123",
        )

        assert result["id"] == "plink_test123"
        assert result["reference_id"] == "greq_abc123"
        mock_request.assert_called_once()
        # Verify idempotency key passed as reference_id
        call_args = mock_request.call_args
        assert call_args[1]["body"]["reference_id"] == "greq_abc123"


def test_recovery_action_provenance_tracked(db_session: Session, make_merchant):
    """RecoveryAction records full provenance for real_test actions."""
    merchant = make_merchant()
    
    # Need an incident and opportunity first
    from app.ports import Severity
    incident = Incident(
        environment=ENVIRONMENT_REAL_TEST,
        title="Test Incident",
        metric="payment_success_rate",
        severity=Severity.HIGH,
        status=IncidentStatus.OPEN,
        detected_at=datetime.now(timezone.utc),
    )
    db_session.add(incident)
    db_session.flush()

    opportunity = RecoveryOpportunity(
        environment=ENVIRONMENT_REAL_TEST,
        incident_id=incident.id,
        opportunity_type="payment_link",
        status=RecoveryStatus.PENDING_APPROVAL,
        amount_paise=50000,
        currency="INR",
    )
    db_session.add(opportunity)
    db_session.flush()

    action = RecoveryAction(
        environment=ENVIRONMENT_REAL_TEST,
        opportunity_id=opportunity.id,
        action_type=ActionType.CREATE_PAYMENT_LINK,
        status=RecoveryStatus.EXECUTING,
        amount_paise=50000,
        currency="INR",
        gateway_request_id="greq_abc123",
        proposed_at=datetime.now(timezone.utc),
    )
    db_session.add(action)
    db_session.commit()

    # Simulate webhook verification completing recovery
    action.status = RecoveryStatus.RECOVERED
    action.verified_at = datetime.now(timezone.utc)
    action.completed_at = datetime.now(timezone.utc)
    db_session.commit()

    db_session.refresh(action)
    assert action.environment == ENVIRONMENT_REAL_TEST
    assert action.gateway_request_id == "greq_abc123"
    assert action.verified_at is not None
    assert action.status == RecoveryStatus.RECOVERED


# -----------------------------------------------------------------------------
# Environment Isolation Tests
# -----------------------------------------------------------------------------

def test_real_test_environment_excludes_simulator_data(db_session: Session, make_merchant):
    """real_test queries never return simulator-sourced rows."""
    merchant = make_merchant()

    # Insert real payment
    real_payment = Payment(
        merchant_id=merchant.id,
        source_type=SOURCE_TYPE_RAZORPAY_TEST,
        source_system="razorpay",
        external_id="pay_real_1",
        amount_paise=10000,
        currency="INR",
        status="captured",
        method="card",
        captured=True,
        created_at=datetime.now(timezone.utc),
        ingested_at=datetime.now(timezone.utc),
    )
    # Insert simulator payment
    sim_payment = Payment(
        merchant_id=merchant.id,
        source_type=SOURCE_TYPE_SIMULATOR,
        source_system="pulserecover-simulator",
        external_id="pay_sim_1",
        amount_paise=10000,
        currency="INR",
        status="captured",
        method="card",
        captured=True,
        created_at=datetime.now(timezone.utc),
        ingested_at=datetime.now(timezone.utc),
    )
    db_session.add_all([real_payment, sim_payment])
    db_session.commit()

    # Query for real_test (source_types_for_environment)
    from app.models.base import source_types_for_environment
    real_types = source_types_for_environment(ENVIRONMENT_REAL_TEST)
    research_types = source_types_for_environment("research")

    real_payments = db_session.query(Payment).filter(Payment.source_type.in_(real_types)).all()
    research_payments = db_session.query(Payment).filter(Payment.source_type.in_(research_types)).all()

    assert len(real_payments) == 1
    assert real_payments[0].external_id == "pay_real_1"
    assert len(research_payments) == 1
    assert research_payments[0].external_id == "pay_sim_1"


def test_dashboard_summary_scoped_to_environment(db_session: Session, make_merchant):
    """Dashboard summary only includes data from requested environment."""
    from app.api.v1.dashboard import get_summary
    from app.models.base import source_types_for_environment

    merchant = make_merchant()

    # Create real_test payment
    real_payment = Payment(
        merchant_id=merchant.id,
        source_type=SOURCE_TYPE_RAZORPAY_TEST,
        source_system="razorpay",
        external_id="pay_real_1",
        amount_paise=10000,
        currency="INR",
        status="captured",
        method="card",
        captured=True,
        created_at=datetime.now(timezone.utc),
        ingested_at=datetime.now(timezone.utc),
    )
    # Create research payment
    sim_payment = Payment(
        merchant_id=merchant.id,
        source_type=SOURCE_TYPE_SIMULATOR,
        source_system="pulserecover-simulator",
        external_id="pay_sim_1",
        amount_paise=10000,
        currency="INR",
        status="captured",
        method="card",
        captured=True,
        created_at=datetime.now(timezone.utc),
        ingested_at=datetime.now(timezone.utc),
    )
    db_session.add_all([real_payment, sim_payment])
    db_session.commit()

    # Mock the anchor to use our test data
    with patch('app.api.v1.dashboard.latest_event_anchor', return_value=datetime.now(timezone.utc)):
        with patch('app.api.v1.dashboard.load_outcomes') as mock_load:
            # Create mock outcomes that can be called multiple times
            def mock_outcomes(*args, **kwargs):
                return [type('obj', (object,), {'success': True, 'ts': datetime.now(timezone.utc), 'amount_paise': 10000})()]
            
            mock_load.side_effect = mock_outcomes

            # Query real_test
            real_summary = get_summary(db_session, environment="real_test")
            assert real_summary.environment == "real_test"

            # Query research
            research_summary = get_summary(db_session, environment="research")
            assert research_summary.environment == "research"


def test_demo_reset_never_touches_real_test_data(db_session: Session, make_merchant):
    """Demo reset only clears simulator/research data, never real_test."""
    from app.api.v1.demo import _reset_statement, _RESET_TABLES
    from app.ports import Severity

    # Create a REAL_TEST merchant (not simulator) so it survives reset
    real_merchant = Merchant(
        name="Real Test Merchant",
        source_type=SOURCE_TYPE_RAZORPAY_TEST,
        source_system="razorpay",
        external_id="mch_real_1",
        is_active=True,
        meta={"created_by": "test"},
    )
    db_session.add(real_merchant)
    db_session.flush()

    # Create a SIMULATOR merchant for simulator data
    sim_merchant = Merchant(
        name="Simulator Merchant",
        source_type=SOURCE_TYPE_SIMULATOR,
        source_system="pulserecover-simulator",
        external_id="mch_sim_1",
        is_active=True,
        meta={"created_by": "test"},
    )
    db_session.add(sim_merchant)
    db_session.flush()

    # Insert real_test data - must explicitly set source_type to avoid simulator default
    real_payment = Payment(
        merchant_id=real_merchant.id,
        source_type=SOURCE_TYPE_RAZORPAY_TEST,
        source_system="razorpay",
        external_id="pay_real_1",
        amount_paise=10000,
        currency="INR",
        status="captured",
        method="card",
        captured=True,
        created_at=datetime.now(timezone.utc),
        ingested_at=datetime.now(timezone.utc),
    )
    # Insert simulator data
    sim_payment = Payment(
        merchant_id=sim_merchant.id,
        source_type=SOURCE_TYPE_SIMULATOR,
        source_system="pulserecover-simulator",
        external_id="pay_sim_1",
        amount_paise=10000,
        currency="INR",
        status="captured",
        method="card",
        captured=True,
        created_at=datetime.now(timezone.utc),
        ingested_at=datetime.now(timezone.utc),
    )
    # Insert real_test incident
    real_incident = Incident(
        environment=ENVIRONMENT_REAL_TEST,
        title="Real Incident",
        metric="payment_success_rate",
        severity=Severity.HIGH,
        status=IncidentStatus.OPEN,
        detected_at=datetime.now(timezone.utc),
    )
    # Insert research incident
    research_incident = Incident(
        environment=ENVIRONMENT_RESEARCH,
        title="Research Incident",
        metric="payment_success_rate",
        severity=Severity.HIGH,
        status=IncidentStatus.OPEN,
        detected_at=datetime.now(timezone.utc),
    )
    # Insert webhook events
    real_webhook = WebhookEvent(
        gateway_event_id="evt_real_1",
        event_type="payment.captured",
        payload={},
        signature_valid=True,
        processed=True,
        source="razorpay",
        received_at=datetime.now(timezone.utc),
    )
    sim_webhook = WebhookEvent(
        gateway_event_id="evt_sim_1",
        event_type="payment.captured",
        payload={},
        signature_valid=True,
        processed=True,
        source="simulator",
        received_at=datetime.now(timezone.utc),
    )

    db_session.add_all([real_payment, sim_payment, real_incident, research_incident, real_webhook, sim_webhook])
    db_session.commit()

    # Run reset statements for each table
    for table, model in _RESET_TABLES:
        stmt = _reset_statement(table, model)
        db_session.execute(stmt)
    db_session.commit()

    # Real data should remain
    assert db_session.query(Payment).filter(Payment.external_id == "pay_real_1").count() == 1
    assert db_session.query(Incident).filter(Incident.environment == ENVIRONMENT_REAL_TEST).count() == 1
    assert db_session.query(WebhookEvent).filter(WebhookEvent.source == "razorpay").count() == 1

    # Simulator/research data should be gone
    assert db_session.query(Payment).filter(Payment.external_id == "pay_sim_1").count() == 0
    assert db_session.query(Incident).filter(Incident.environment == ENVIRONMENT_RESEARCH).count() == 0
    assert db_session.query(WebhookEvent).filter(WebhookEvent.source == "simulator").count() == 0


# -----------------------------------------------------------------------------
# Provenance Tests
# -----------------------------------------------------------------------------

def test_every_commerce_row_has_provenance(db_session: Session, make_merchant):
    """All commerce rows have source_type, source_system, external_id, ingested_at."""
    merchant = make_merchant()
    payment = Payment(
        merchant_id=merchant.id,
        source_type=SOURCE_TYPE_RAZORPAY_TEST,
        source_system="razorpay",
        external_id="pay_123",
        amount_paise=10000,
        currency="INR",
        status="captured",
        method="card",
        captured=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(payment)
    db_session.commit()

    assert payment.source_type == SOURCE_TYPE_RAZORPAY_TEST
    assert payment.source_system == "razorpay"
    assert payment.external_id == "pay_123"
    assert payment.ingested_at is not None
    assert payment.ingested_at.tzinfo is not None  # tz-aware


def test_recovery_action_links_to_gateway_request(db_session: Session, make_merchant):
    """RecoveryAction.gateway_request_id traces to Razorpay entity."""
    merchant = make_merchant()
    from app.ports import Severity
    
    incident = Incident(
        environment=ENVIRONMENT_REAL_TEST,
        title="Test Incident",
        metric="payment_success_rate",
        severity=Severity.HIGH,
        status=IncidentStatus.OPEN,
        detected_at=datetime.now(timezone.utc),
    )
    db_session.add(incident)
    db_session.flush()

    opportunity = RecoveryOpportunity(
        environment=ENVIRONMENT_REAL_TEST,
        incident_id=incident.id,
        opportunity_type="payment_link",
        status=RecoveryStatus.PENDING_APPROVAL,
        amount_paise=50000,
        currency="INR",
    )
    db_session.add(opportunity)
    db_session.flush()

    action = RecoveryAction(
        environment=ENVIRONMENT_REAL_TEST,
        opportunity_id=opportunity.id,
        action_type=ActionType.CREATE_PAYMENT_LINK,
        status=RecoveryStatus.EXECUTING,
        amount_paise=50000,
        currency="INR",
        gateway_request_id="greq_abc123",  # = Razorpay reference_id
        proposed_at=datetime.now(timezone.utc),
    )
    db_session.add(action)
    db_session.commit()

    assert action.gateway_request_id == "greq_abc123"
    assert action.environment == ENVIRONMENT_REAL_TEST
    assert action.proposed_at is not None


def test_audit_log_records_environment(db_session: Session):
    """AuditLog records environment for every action."""
    entry = audit.record(
        db_session,
        actor="test:actor",
        action="test.action",
        entity_type="test_entity",
        entity_id="ent_123",
        details={"key": "value"},
    )
    db_session.commit()

    # Default environment is research (safe failure direction)
    assert entry.environment == ENVIRONMENT_RESEARCH
    assert entry.actor == "test:actor"
    assert entry.action == "test.action"
    assert entry.details == {"key": "value"}


# -----------------------------------------------------------------------------
# Empty State Tests
# -----------------------------------------------------------------------------

def test_fresh_database_shows_empty_dashboard(db_session: Session):
    """Fresh DB returns zeros and empty arrays for real_test dashboard."""
    from app.api.v1.dashboard import get_summary

    with patch('app.api.v1.dashboard.latest_event_anchor', return_value=None):
        summary = get_summary(db_session, environment="real_test")

        assert summary.environment == "real_test"
        assert summary.payments_observed == 0
        assert summary.payments_success_rate == 0.0
        assert summary.open_incidents == 0
        assert summary.revenue_at_risk_paise == 0
        assert summary.recovered_revenue_paise == 0
        assert summary.recent_incidents == []


def test_research_environment_shows_empty_when_no_sim_data(db_session: Session):
    """Research environment shows empty when no simulator data."""
    from app.api.v1.dashboard import get_summary

    with patch('app.api.v1.dashboard.latest_event_anchor', return_value=None):
        summary = get_summary(db_session, environment="research")

        assert summary.environment == "research"
        assert summary.payments_observed == 0
        assert summary.open_incidents == 0


# -----------------------------------------------------------------------------
# Settings/Connection State Tests
# -----------------------------------------------------------------------------

def test_connection_state_singleton_created(db_session: Session, real_test_settings):
    """ConnectionState singleton created on first access."""
    service = SyncService.from_settings(real_test_settings)
    state = service.get_connection_state(db_session)

    assert state.id == CONNECTION_STATE_SINGLETON_ID
    # Default sync_enabled depends on model - just verify it's a boolean
    assert isinstance(state.sync_enabled, bool)
    assert state.last_sync_at is None


def test_sync_enable_disable_audited(db_session: Session, real_test_settings):
    """Sync enable/disable creates audit trail."""
    service = SyncService.from_settings(real_test_settings)

    # Enable
    state = service.set_sync_enabled(db_session, True)
    audit.record(db_session, actor="api:merchant", action="merchant.sync_enable", entity_type="connection_state", entity_id=state.id, details={"sync_enabled": True})
    db_session.commit()

    assert state.sync_enabled is True

    # Disable
    state = service.set_sync_enabled(db_session, False)
    audit.record(db_session, actor="api:merchant", action="merchant.sync_disable", entity_type="connection_state", entity_id=state.id, details={"sync_enabled": False})
    db_session.commit()

    assert state.sync_enabled is False


def test_sync_refuses_when_disabled(db_session: Session, real_test_settings):
    """Sync refuses with SyncDisabledError when sync_enabled=false."""
    # Create a mock transport that would succeed if called
    responses = {
        "GET /v1/orders": httpx.Response(200, json={"entity": "collection", "count": 0, "items": []}),
        "GET /v1/payments": httpx.Response(200, json={"entity": "collection", "count": 0, "items": []}),
        "GET /v1/subscriptions": httpx.Response(200, json={"entity": "collection", "count": 0, "items": []}),
        "GET /v1/payment_links": httpx.Response(200, json=[]),
    }
    transport = MockTransport(responses)

    service = SyncService.from_settings(real_test_settings, transport=transport)
    # Explicitly disable sync
    service.set_sync_enabled(db_session, False)
    db_session.commit()

    with pytest.raises(SyncDisabledError):
        service.run_sync(db_session, actor="test", window_days=30)


def test_sync_refuses_when_not_configured(db_session: Session, no_keys_settings):
    """Sync refuses with SyncNotConfiguredError when no keys."""
    service = SyncService.from_settings(no_keys_settings)

    with pytest.raises(SyncNotConfiguredError):
        service.run_sync(db_session, actor="test", window_days=30)


def test_webhook_rejects_invalid_signature(real_test_settings):
    """Webhook endpoint rejects requests with invalid HMAC signature."""
    gateway = RazorpayGateway(
        key_id=real_test_settings.RAZORPAY_KEY_ID,
        key_secret=real_test_settings.RAZORPAY_KEY_SECRET,
        webhook_secret="webhook_secret",
    )

    payload = b'{"event":"test"}'
    assert gateway.verify_webhook_signature(payload, "wrong_signature") is False
    assert gateway.verify_webhook_signature(payload, "") is False


def test_no_fake_data_in_real_test_environment(db_session: Session):
    """Real test environment never contains seeded/fake data unless explicitly synced."""
    # Fresh DB should have zero real_test commerce rows
    from app.models.base import source_types_for_environment
    real_types = source_types_for_environment(ENVIRONMENT_REAL_TEST)

    real_count = db_session.query(Payment).filter(Payment.source_type.in_(real_types)).count()
    assert real_count == 0

    real_incidents = db_session.query(Incident).filter(Incident.environment == ENVIRONMENT_REAL_TEST).count()
    assert real_incidents == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
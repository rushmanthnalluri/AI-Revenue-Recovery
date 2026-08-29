"""Provenance tagging: every commerce row carries an honest source story
(docs/data-provenance.md).

- Simulator engine rows -> source_type='simulator',
  source_system='pulserecover-simulator', external_id = the row's
  deterministic gateway id.
- Webhook-appended payment_events -> tagged by the configured gateway:
  simulated gateway -> 'simulator'; real Razorpay Test Mode ->
  'razorpay_test' / 'razorpay'.
- ORM rows without explicit provenance default to 'simulator' (honest: the
  simulator is the only commerce-row writer in the codebase).
- payments enforce UNIQUE (source_type, external_id) — one upstream payment
  id can never be double-stored under the same source.
- The f3a9c1e7b204 migration upgrades, backfills, and downgrades cleanly.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.models as models
from app.api.deps import get_gateway_dependency
from app.config import settings
from app.db import get_db
from app.main import create_app
from app.services.razorpay.simulated import SimulatedPaymentGateway
from app.services.recovery.webhook_handlers import dispatch_event
from app.simulator import run_simulation
from tests.simulator.conftest import fresh_session, small_config

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
BASE_REVISION = "77c0efef3d84"  # pre-provenance initial schema

_COMMERCE_MODELS = (
    models.Merchant,
    models.Customer,
    models.Order,
    models.Payment,
    models.PaymentEvent,
    models.Subscription,
)
# model -> gateway-id column that external_id must mirror (None: no upstream id)
_EXTERNAL_ID_MIRROR = {
    models.Merchant: "gateway_account_id",
    models.Customer: "gateway_customer_id",
    models.Order: "gateway_order_id",
    models.Payment: "gateway_payment_id",
    models.PaymentEvent: None,
    models.Subscription: "gateway_subscription_id",
}


# ---------------------------------------------------------------------------
# simulator path
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sim_session():
    """One small simulation shared by the tagging assertions."""
    session = fresh_session()
    run_simulation(small_config(target_events=2_000, customers=20, days=2), session)
    try:
        yield session
    finally:
        session.close()


def test_simulator_rows_are_tagged(sim_session: Session) -> None:
    for model in _COMMERCE_MODELS:
        rows = list(
            sim_session.execute(
                sa.select(model.source_type, model.source_system, model.ingested_at)
            ).all()
        )
        assert rows, f"expected {model.__name__} rows from the simulation"
        for source_type, source_system, ingested_at in rows:
            assert source_type == "simulator", model.__name__
            assert source_system == "pulserecover-simulator", model.__name__
            assert ingested_at is not None, model.__name__
            assert ingested_at.tzinfo is not None, model.__name__


def test_simulator_external_id_mirrors_gateway_id(sim_session: Session) -> None:
    for model, gw_col in _EXTERNAL_ID_MIRROR.items():
        if gw_col is None:
            n = sim_session.scalar(
                sa.select(sa.func.count())
                .select_from(model)
                .where(model.external_id.is_not(None))
            )
            assert n == 0, f"{model.__name__} has no upstream id to mirror"
            continue
        mismatched = sim_session.scalar(
            sa.select(sa.func.count())
            .select_from(model)
            .where(
                sa.or_(
                    model.external_id.is_(None),
                    model.external_id != getattr(model, gw_col),
                )
            )
        )
        assert mismatched == 0, f"{model.__name__}.external_id must mirror {gw_col}"


# ---------------------------------------------------------------------------
# webhook path
# ---------------------------------------------------------------------------


def _payment_event(db: Session, payment_id: str, source: str) -> models.PaymentEvent:
    ev = db.scalar(
        sa.select(models.PaymentEvent)
        .where(models.PaymentEvent.payment_id == payment_id, models.PaymentEvent.source == source)
        .order_by(models.PaymentEvent.created_at.desc())
        .limit(1)
    )
    assert ev is not None, f"no {source!r} event stored for {payment_id}"
    return ev


def test_webhook_event_via_sim_gateway_stays_simulator(db_session: Session, make_payment) -> None:
    """A genuinely-signed delivery from the SIMULATED gateway must be tagged
    'simulator' — simulation is never hidden."""
    gateway = SimulatedPaymentGateway(webhook_secret="whsec_prov_sim")
    app = create_app()

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gateway_dependency] = lambda: gateway

    p = make_payment(gateway_payment_id="pay_prov_sim1", status="created")
    entity = {
        "id": "pay_prov_sim1",
        "entity": "payment",
        "amount": 50000,
        "currency": "INR",
        "status": "captured",
        "method": "upi",
        "captured": True,
    }
    body, signature, event_id = gateway.build_event("payment.captured", entity)
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "content-type": "application/json",
                "x-razorpay-signature": signature,
                "x-razorpay-event-id": event_id,
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["processed"] is True

    ev = _payment_event(db_session, p.id, "webhook")
    assert ev.source_type == "simulator"
    assert ev.source_system == "pulserecover-simulator"
    assert ev.external_id == "pay_prov_sim1"
    assert ev.ingested_at is not None and ev.ingested_at.tzinfo is not None


def test_webhook_event_in_real_mode_tagged_razorpay_test(
    db_session: Session, make_payment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With SIMULATION_MODE=false and keys configured (real Razorpay Test
    Mode), webhook-derived event rows must be tagged 'razorpay_test'."""
    monkeypatch.setattr(settings, "SIMULATION_MODE", False)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_prov")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "prov_secret")

    p = make_payment(gateway_payment_id="pay_prov_real1", status="created")
    payload = {
        "entity": "event",
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_prov_real1",
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
    processed, detail = dispatch_event(db_session, "payment.captured", payload)
    db_session.commit()
    assert processed, detail

    ev = _payment_event(db_session, p.id, "webhook")
    assert ev.source_type == "razorpay_test"
    assert ev.source_system == "razorpay"
    assert ev.external_id == "pay_prov_real1"


# ---------------------------------------------------------------------------
# defaults + dedup
# ---------------------------------------------------------------------------


def test_orm_rows_default_to_simulator_provenance(make_payment) -> None:
    """A row written without explicit provenance is honestly 'simulator' —
    the simulator is the only commerce-row writer that exists."""
    p = make_payment(gateway_payment_id="pay_prov_default")
    assert p.source_type == "simulator"
    assert p.source_system is None  # no claim we cannot prove
    assert p.external_id is None
    assert p.ingested_at is not None and p.ingested_at.tzinfo is not None


def test_payments_external_id_dedup_is_scoped_to_source(
    db_session: Session, make_merchant
) -> None:
    merchant = make_merchant()

    def _payment(source_type: str, external_id: str) -> models.Payment:
        return models.Payment(
            merchant_id=merchant.id,
            amount_paise=1000,
            source_type=source_type,
            external_id=external_id,
        )

    db_session.add(_payment("simulator", "pay_prov_dup"))
    db_session.commit()

    # Same source + same upstream id -> rejected (no double-storage).
    db_session.add(_payment("simulator", "pay_prov_dup"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Same upstream id under a DIFFERENT source -> allowed (sim row and a
    # future real-ingested row for the same gateway payment can coexist).
    db_session.add(_payment("razorpay_test", "pay_prov_dup"))
    db_session.commit()


# ---------------------------------------------------------------------------
# migration: upgrade -> backfill -> downgrade -> re-upgrade
# ---------------------------------------------------------------------------


def test_migration_upgrade_backfill_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "prov_migration.db"
    url = f"sqlite:///{db_file.as_posix()}"
    # alembic env.py reads the URL from app settings — point it at scratch.
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    cfg = Config(str(ALEMBIC_INI))

    # Pre-provenance schema, then one legacy merchant + payment via raw SQL
    # (the current ORM already knows the new columns).
    command.upgrade(cfg, BASE_REVISION)
    engine = sa.create_engine(url)
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO merchants (id, name, gateway_account_id, is_active, meta,"
                " created_at, updated_at)"
                " VALUES ('mch_legacy', 'Legacy Co', 'acc_sim000007', 1, '{}', :n, :n)"
            ),
            {"n": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO payments (id, merchant_id, gateway_payment_id, amount_paise,"
                " currency, status, captured, attempts, meta, created_at, updated_at)"
                " VALUES ('pay_legacy', 'mch_legacy', 'pay_S7_legacy', 1000, 'INR',"
                " 'failed', 0, 1, '{}', :n, :n)"
            ),
            {"n": now},
        )

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT source_type, source_system, external_id, ingested_at"
                " FROM payments WHERE id = 'pay_legacy'"
            )
        ).one()
        assert row[0] == "simulator"  # pre-provenance rows honestly tagged
        assert row[1] == "pulserecover-simulator"
        assert row[2] == "pay_S7_legacy"  # external_id mirrors gateway id
        assert row[3] is not None  # stamped at migration time
        mrow = conn.execute(
            sa.text("SELECT source_system, external_id FROM merchants WHERE id = 'mch_legacy'")
        ).one()
        assert mrow == ("pulserecover-simulator", "acc_sim000007")
        ddl = conn.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE name = 'payments'")
        ).scalar()
        assert "uq_payments_source_external" in ddl
        assert "ingested_at" in ddl and "NOT NULL" in ddl

    command.downgrade(cfg, BASE_REVISION)
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(payments)"))}
        assert not ({"source_type", "source_system", "external_id", "ingested_at"} & cols)
        assert conn.execute(sa.text("SELECT count(*) FROM payments")).scalar_one() == 1

    # Re-upgrade is clean (downgrade genuinely reversed the schema).
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT source_type, external_id FROM payments WHERE id = 'pay_legacy'")
        ).one()
        assert row == ("simulator", "pay_S7_legacy")
    engine.dispose()

"""Environment-isolation fixtures: builders for real_test-tagged rows.

The root tests/conftest.py provides db_session/client/make_merchant/
make_payment/make_incident; factories here stamp the real_test environment
(Razorpay Test Mode provenance) so tests can mix both environments in one DB.
"""

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

import app.models as models
from app.db import utcnow
from app.ports import RecoveryStatus

# Re-export the detection suite's deterministic seeders as fixtures for this
# package (importing a fixture into a conftest registers it here).
from tests.detection.conftest import (  # noqa: F401
    Stream,
    seed_payment_events,
)


@pytest.fixture()
def make_real_payment(db_session: Session, make_merchant):
    """A payment with Razorpay Test Mode provenance (real_test environment)."""

    def _make(merchant=None, **kw) -> models.Payment:
        if merchant is None:
            merchant = make_merchant(
                source_type="razorpay_test",
                source_system="razorpay",
                gateway_account_id="acc_realtest1",
            )
        kw.setdefault("source_type", "razorpay_test")
        kw.setdefault("source_system", "razorpay")
        payment = models.Payment(
            merchant_id=merchant.id,
            amount_paise=kw.pop("amount_paise", 50_000),
            status=kw.pop("status", "failed"),
            **kw,
        )
        db_session.add(payment)
        db_session.commit()
        return payment

    return _make


@pytest.fixture()
def make_opportunity(db_session: Session, make_incident):
    """Opportunity factory with an explicit environment stamp."""

    def _make(
        *, incident=None, environment: str = "research", **kw
    ) -> models.RecoveryOpportunity:
        if incident is None:
            incident = make_incident(environment=environment)
        opp = models.RecoveryOpportunity(
            incident_id=incident.id,
            opportunity_type=kw.pop("opportunity_type", "failed_payment_retry"),
            status=kw.pop("status", RecoveryStatus.PROPOSED),
            amount_paise=kw.pop("amount_paise", 100_000),
            environment=environment,
            **kw,
        )
        db_session.add(opp)
        db_session.commit()
        return opp

    return _make


@pytest.fixture()
def seed_sim_payments(db_session: Session, make_merchant):
    """A small simulator-provenance payment stream (8 captured + 2 failed in
    the last hour, quiet before) — mirrors tests/integration/test_dashboard."""

    def _seed(*, count: int = 10) -> models.Merchant:
        merchant = make_merchant(name="Env isolation merchant")
        now = utcnow().replace(microsecond=0)
        for i in range(count):
            status = "failed" if i >= count - 2 else "captured"
            payment = models.Payment(
                merchant_id=merchant.id,
                amount_paise=10_000,
                currency="INR",
                status=status,
                captured=status == "captured",
                method="upi",
            )
            db_session.add(payment)
            db_session.flush()
            db_session.add(
                models.PaymentEvent(
                    payment_id=payment.id,
                    event_type=f"payment.{status}",
                    to_status=status,
                    source="seed",
                    occurred_at=now - timedelta(minutes=10),
                )
            )
        db_session.commit()
        return merchant

    return _seed

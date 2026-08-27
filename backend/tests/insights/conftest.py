"""Insights test fixtures: deterministic facet-tagged payment seeding.

Exact counts, no randomness — every expected rate/lift in the assertions is
computable by hand from the seeded numbers. Fixtures are SMALL on purpose:
they are unit fixtures, not the simulator.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Merchant, Payment, PaymentEvent

EPOCH = datetime(2026, 2, 2, 12, 0, 0, tzinfo=timezone.utc)  # fixed, tz-aware
WINDOW_START = EPOCH
WINDOW_END = EPOCH + timedelta(hours=1)
BASELINE_START = EPOCH - timedelta(hours=1)  # equal-duration, immediately prior

BAD_REQUEST = "BAD_REQUEST_ERROR"

TS_BASELINE = BASELINE_START + timedelta(minutes=30)
TS_WINDOW = WINDOW_START + timedelta(minutes=15)


@pytest.fixture()
def insights_merchant(db_session: Session) -> Merchant:
    merchant = Merchant(name="Insights Test Merchant")
    db_session.add(merchant)
    db_session.commit()
    return merchant


@pytest.fixture()
def add_outcome(db_session: Session):
    """Add one payment with a terminal event at ``ts`` carrying facet telemetry."""

    def _add(
        merchant: Merchant,
        *,
        ts: datetime,
        method: str = "upi",
        bank: str = "hdfc",
        gateway: str = "razorpay",
        success: bool = True,
        error_code: str | None = None,
        error_reason: str | None = None,
    ) -> Payment:
        status = "captured" if success else "failed"
        meta = {"bank": bank, "gateway": gateway}
        if not success and error_reason:
            meta["error_reason"] = error_reason
        payment = Payment(
            merchant_id=merchant.id,
            amount_paise=10000,
            status=status,
            method=method,
            error_code=None if success else error_code,
            meta=meta,
            gateway_created_at=ts,
        )
        db_session.add(payment)
        db_session.flush()
        payload = {}
        if not success:
            if error_code:
                payload["error_code"] = error_code
            if error_reason:
                payload["error_reason"] = error_reason
        db_session.add(
            PaymentEvent(
                payment_id=payment.id,
                event_type=f"payment.{status}",
                to_status=status,
                source="seed",
                payload=payload,
                occurred_at=ts,
            )
        )
        return payment

    return _add


def seed_mix(
    add,
    merchant: Merchant,
    *,
    ts: datetime,
    method: str,
    bank: str,
    n_success: int,
    failures: list[tuple[str, str]],  # (error_code, error_reason) per failure
) -> None:
    """Seed a homogeneous slice: n_success captures + one failure per spec."""
    for _ in range(n_success):
        add(merchant, ts=ts, method=method, bank=bank, success=True)
    for code, reason in failures:
        add(
            merchant,
            ts=ts,
            method=method,
            bank=bank,
            success=False,
            error_code=code,
            error_reason=reason,
        )

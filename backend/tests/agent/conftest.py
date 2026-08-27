"""Fixtures for agent tests: a seeded incident with a failure-heavy window.

Layout (all times UTC):
- T0 .. T0+1h: the incident window — 10 failed payments (bank technical
  errors dominate), 3 captured.
- T0-1h .. T0: the diagnosis/stats baseline — mostly captured, one non-bank
  failure.
- T0-3d .. T0-1h: extra captured history so the revenue engine's 7-day
  baseline has signal.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

import app.models as models
from app.ports import Severity

T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
WINDOW = (T0, T0 + timedelta(hours=1))


def _add_payment(
    db: Session,
    merchant,
    customer,
    *,
    status: str,
    when: datetime,
    amount_paise: int = 50_000,
    method: str = "upi",
    error_source: str | None = None,
    error_reason: str | None = None,
) -> models.Payment:
    payment = models.Payment(
        merchant_id=merchant.id,
        customer_id=customer.id if customer else None,
        amount_paise=amount_paise,
        status=status,
        method=method,
        error_code=("BAD_REQUEST_ERROR" if status == "failed" else None),
        error_source=error_source,
        captured=status == "captured",
        meta={"error_reason": error_reason} if error_reason else {},
        created_at=when,
    )
    db.add(payment)
    db.flush()
    db.add(
        models.PaymentEvent(
            payment_id=payment.id,
            event_type=f"payment.{status}",
            to_status=status,
            source="simulator",
            occurred_at=when,
            payload={
                "bank": "TESTBANK" if error_source == "bank" else None,
                "error_source": error_source,
                "error_step": "payment_authorization" if status == "failed" else None,
                "error_reason": error_reason,
                "latency_ms": None,
                "subscription_id": None,
            },
        )
    )
    db.flush()
    return payment


@pytest.fixture()
def agent_seed(db_session: Session, make_merchant, make_incident):
    """Seed the scenario; returns a dict with the ORM rows tests need."""
    merchant = make_merchant()
    customer = models.Customer(merchant_id=merchant.id, email="cust@example.com")
    db_session.add(customer)
    db_session.flush()

    # Deep baseline for the revenue engine (7-day lookback).
    for i in range(24):
        _add_payment(
            db_session,
            merchant,
            customer if i % 3 == 0 else None,
            status="captured",
            when=T0 - timedelta(days=2, minutes=i * 37),
            amount_paise=40_000 + i * 1_000,
        )
    # Immediate 1h baseline (diagnosis + payment-stats compare against this).
    for i in range(9):
        _add_payment(
            db_session,
            merchant,
            customer if i % 4 == 0 else None,
            status="captured",
            when=T0 - timedelta(minutes=55 - i * 5),
            amount_paise=45_000,
        )
    _add_payment(
        db_session,
        merchant,
        None,
        status="failed",
        when=T0 - timedelta(minutes=10),
        error_source="customer",
        error_reason="incorrect_otp",
    )

    # Incident window: 10 failed (6 bank technical), 3 captured.
    failed = []
    for i in range(6):
        failed.append(
            _add_payment(
                db_session,
                merchant,
                customer if i < 2 else None,
                status="failed",
                when=T0 + timedelta(minutes=2 + i * 4),
                amount_paise=60_000 + i * 10_000,
                error_source="bank",
                error_reason="bank_technical_error",
            )
        )
    for i, reason in enumerate(("insufficient_fund", "payment_cancelled", "card_declined", "bank_technical_error")):
        failed.append(
            _add_payment(
                db_session,
                merchant,
                customer if i == 0 else None,
                status="failed",
                when=T0 + timedelta(minutes=30 + i * 3),
                amount_paise=25_000,
                method="card" if reason == "card_declined" else "upi",
                error_source="customer" if reason in ("insufficient_fund", "payment_cancelled") else "bank",
                error_reason=reason,
            )
        )
    for i in range(3):
        _add_payment(
            db_session,
            merchant,
            None,
            status="captured",
            when=T0 + timedelta(minutes=10 + i * 7),
            amount_paise=30_000,
        )

    incident = make_incident(
        title="UPI success rate drop",
        metric="payment_success_rate",
        severity=Severity.HIGH,
        baseline_value=0.9,
        observed_value=0.23,
        deviation_pct=-74.4,
        window_start=WINDOW[0],
        window_end=WINDOW[1],
        detected_at=WINDOW[1],
    )
    db_session.commit()
    return {
        "merchant": merchant,
        "customer": customer,
        "incident": incident,
        "failed_payments": failed,
        "top_failed": max(failed, key=lambda p: p.amount_paise),
    }


@pytest.fixture()
def empty_incident(db_session: Session, make_incident):
    """An incident whose window contains no payments at all."""
    return make_incident(
        title="Quiet window",
        metric="payment_success_rate",
        severity=Severity.LOW,
        window_start=WINDOW[0],
        window_end=WINDOW[1],
        detected_at=WINDOW[1],
    )

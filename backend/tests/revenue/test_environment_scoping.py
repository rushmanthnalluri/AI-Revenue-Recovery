"""Environment scoping for revenue_at_risk (the real_test/research boundary).

Both environments plant OVERLAPPING payments (same method/amount/timestamps,
different source_type); the report for an incident must reflect ONLY its own
environment's commerce rows — baseline, incident window, and the
new-vs-returning segmentation alike (docs/data-provenance.md).
"""

from datetime import datetime, timedelta, timezone

import pytest

import app.models as models
from app.services.revenue import RevenueService

EPOCH = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
WINDOW_START = EPOCH
WINDOW_END = EPOCH + timedelta(hours=1)
# Inside the 7-day baseline window that precedes the incident window.
TS_BASELINE = EPOCH - timedelta(days=1)
# Before the baseline window — the "returning customer" lookback.
TS_LONG_AGO = EPOCH - timedelta(days=30)


def _plant(
    db_session,
    merchant,
    *,
    source_type: str,
    n: int,
    ts: datetime,
    status: str,
    amount_paise: int = 100_000,
    method: str = "upi",
    customer_id: str | None = None,
) -> list[models.Payment]:
    payments = []
    for i in range(n):
        p = models.Payment(
            merchant_id=merchant.id,
            amount_paise=amount_paise,
            method=method,
            status=status,
            captured=status == "captured",
            customer_id=customer_id,
            source_type=source_type,
            created_at=ts + timedelta(seconds=i),
        )
        db_session.add(p)
        payments.append(p)
    db_session.commit()
    return payments


def _totals(report) -> tuple[int, int, int]:
    """(attempted_count, attempted_amount_paise, failed_count) over segments."""
    return (
        sum(s.attempted_count for s in report.segments),
        sum(s.attempted_amount_paise for s in report.segments),
        sum(s.failed_count for s in report.segments),
    )


@pytest.fixture()
def overlapping_payments(db_session, make_merchant):
    """Same shape in both environments: 40 baseline (30 captured) + 6 window
    payments, so any cross-environment leak changes the segment totals."""
    merchant = make_merchant()
    for source_type in ("simulator", "razorpay_test"):
        _plant(db_session, merchant, source_type=source_type, n=30, ts=TS_BASELINE, status="captured")
        _plant(db_session, merchant, source_type=source_type, n=10, ts=TS_BASELINE, status="failed")
        _plant(db_session, merchant, source_type=source_type, n=6, ts=WINDOW_START, status="failed")
    return merchant


def _incident(db_session, make_incident, environment: str) -> models.Incident:
    return make_incident(
        environment=environment,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        detected_at=WINDOW_END,
    )


def test_research_incident_ignores_real_test_payments(
    db_session, make_incident, overlapping_payments
):
    incident = _incident(db_session, make_incident, "research")
    report = RevenueService(db_session).revenue_at_risk(incident.id)
    attempted, amount, failed = _totals(report)
    # Only the 6 simulator window payments count (mixing would give 12).
    assert (attempted, failed) == (6, 6)
    assert amount == 600_000
    # Baseline also scoped: the simulator-only baseline rate is 30/40.
    assert all(s.baseline_n == 40 for s in report.segments)


def test_real_test_incident_ignores_research_payments(
    db_session, make_incident, overlapping_payments
):
    incident = _incident(db_session, make_incident, "real_test")
    report = RevenueService(db_session).revenue_at_risk(incident.id)
    attempted, amount, failed = _totals(report)
    assert (attempted, failed) == (6, 6)
    assert amount == 600_000


def test_returning_customer_segmentation_is_environment_scoped(
    db_session, make_merchant, make_customer, make_incident
):
    """A customer captured long ago in real_test is 'returning' only in the
    real_test report; the same customer id in research is 'new' there."""
    merchant = make_merchant()
    customer = make_customer(merchant=merchant)
    _plant(
        db_session,
        merchant,
        source_type="razorpay_test",
        n=1,
        ts=TS_LONG_AGO,
        status="captured",
        customer_id=customer.id,
    )
    _plant(
        db_session,
        merchant,
        source_type="razorpay_test",
        n=1,
        ts=WINDOW_START,
        status="failed",
        customer_id=customer.id,
    )
    _plant(
        db_session,
        merchant,
        source_type="simulator",
        n=1,
        ts=WINDOW_START,
        status="failed",
        customer_id=customer.id,
    )

    real_report = RevenueService(db_session).revenue_at_risk(
        _incident(db_session, make_incident, "real_test").id
    )
    assert {s.customer_type for s in real_report.segments} == {"returning"}

    research_report = RevenueService(db_session).revenue_at_risk(
        _incident(db_session, make_incident, "research").id
    )
    assert {s.customer_type for s in research_report.segments} == {"new"}

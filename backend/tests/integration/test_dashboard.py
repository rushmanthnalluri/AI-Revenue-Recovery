"""Dashboard integration: empty DB yields zeros + empty series (never
fabricated numbers); seeded DB yields real aggregates."""

from datetime import timedelta

import sqlalchemy as sa

from app.db import utcnow
from app.models import Merchant, Payment, PaymentEvent


def test_dashboard_empty_db(client):
    r = client.get("/api/v1/dashboard/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["open_incidents"] == 0
    assert body["revenue_at_risk_paise"] == 0
    assert body["recovered_revenue_paise"] == 0
    assert body["recovery_rate"] == 0.0
    assert body["payments_observed"] == 0
    assert body["payments_baseline_success_rate"] is None
    assert body["recent_incidents"] == []

    r = client.get("/api/v1/dashboard/timeseries")
    assert r.status_code == 200
    assert r.json()["points"] == []

    r = client.get("/api/v1/dashboard/timeseries", params={"metric": "bogus"})
    assert r.status_code == 400


def test_dashboard_reflects_seeded_payments(client, db_session):
    merchant = Merchant(name="Dashboard merchant")
    db_session.add(merchant)
    db_session.flush()
    now = utcnow().replace(microsecond=0)
    # 8 captured + 2 failed in the last hour; quiet 24h before that.
    for i in range(10):
        status = "failed" if i >= 8 else "captured"
        payment = Payment(
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
            PaymentEvent(
                payment_id=payment.id,
                event_type=f"payment.{status}",
                to_status=status,
                source="seed",
                occurred_at=now - timedelta(minutes=10),
            )
        )
    db_session.commit()

    r = client.get("/api/v1/dashboard/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["payments_observed"] == 10
    assert body["payments_success_rate"] == 0.8
    assert body["payments_baseline_success_rate"] is None  # no baseline traffic

    r = client.get(
        "/api/v1/dashboard/timeseries",
        params={"metric": "payments_failed", "granularity": "hour", "window_hours": 2},
    )
    assert r.status_code == 200
    points = r.json()["points"]
    assert sum(p["value"] for p in points) == 2

"""Environment isolation (a): seeded simulator data must be invisible on the
real_test dashboard and fully present on the research dashboard — and vice
versa for real_test-tagged rows. No cross-environment leakage, even through
the aggregate joins (payments via source_type, derived rows via environment).
"""


def test_real_test_dashboard_is_zero_over_simulator_data(client, seed_sim_payments, make_incident):
    seed_sim_payments()
    make_incident(environment="research")

    r = client.get("/api/v1/dashboard/summary")  # default environment is real_test
    assert r.status_code == 200
    body = r.json()
    assert body["environment"] == "real_test"
    assert body["payments_observed"] == 0
    assert body["payments_success_rate"] == 0.0
    assert body["payments_baseline_success_rate"] is None
    assert body["open_incidents"] == 0
    assert body["incidents_by_severity"] == {}
    assert body["revenue_at_risk_paise"] == 0
    assert body["recovered_revenue_paise"] == 0
    assert body["lost_revenue_paise"] == 0
    assert body["recovery_rate"] == 0.0
    assert body["active_recoveries"] == 0
    assert body["pending_approvals"] == 0
    assert body["recent_incidents"] == []

    r = client.get("/api/v1/dashboard/timeseries", params={"metric": "payments_total"})
    assert r.status_code == 200
    assert r.json()["environment"] == "real_test"
    assert r.json()["points"] == []


def test_research_dashboard_sees_the_simulator_data(client, seed_sim_payments):
    seed_sim_payments()

    r = client.get("/api/v1/dashboard/summary", params={"environment": "research"})
    assert r.status_code == 200
    body = r.json()
    assert body["environment"] == "research"
    assert body["payments_observed"] == 10
    assert body["payments_success_rate"] == 0.8

    r = client.get(
        "/api/v1/dashboard/timeseries",
        params={"metric": "payments_failed", "environment": "research"},
    )
    assert sum(p["value"] for p in r.json()["points"]) == 2


def test_each_environment_sees_only_its_own_payments(
    client, db_session, seed_sim_payments, make_real_payment
):
    """Mixed DB: the real_test aggregate counts ONLY razorpay_test rows and
    the research aggregate ONLY simulator rows (no join sloppiness)."""
    from datetime import timedelta

    import app.models as models
    from app.db import utcnow

    seed_sim_payments()
    real = make_real_payment(status="captured", captured=True)
    db_session.add(
        models.PaymentEvent(
            payment_id=real.id,
            event_type="payment.captured",
            to_status="captured",
            source="webhook",
            occurred_at=utcnow() - timedelta(minutes=5),
        )
    )
    db_session.commit()

    body = client.get("/api/v1/dashboard/summary").json()  # real_test default
    assert body["payments_observed"] == 1
    assert body["payments_success_rate"] == 1.0

    body = client.get(
        "/api/v1/dashboard/summary", params={"environment": "research"}
    ).json()
    assert body["payments_observed"] == 10  # unchanged: the real row is invisible
    assert body["payments_success_rate"] == 0.8

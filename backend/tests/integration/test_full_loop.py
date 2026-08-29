"""Full-loop integration test: simulator scenario -> detection -> incident
detail (auto-diagnosis + revenue-at-risk) -> opportunity build -> policy-gated
execution -> webhook verification -> dashboard recovered revenue.

Runs against the real services with a SimulatedPaymentGateway; nothing is
mocked except the gateway twin (which is the point of the simulation mode).
"""

import sqlalchemy as sa

from app.models import AuditLog, Payment, RecoveryAction
from app.ports import RecoveryStatus
from app.services.razorpay.simulated import SimulatedPaymentGateway

API_KEY = {"X-API-Key": "dev-key"}


def _approve_and_execute(client, opp_id: str, strategy_id: str) -> dict:
    """Execute, taking the human-approval lane when the policy gate asks."""
    r = client.post(
        f"/api/v1/recovery/{opp_id}/execute",
        json={"strategy_id": strategy_id, "actor": "human:integration-test"},
        headers=API_KEY,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    if body["status"] == "PENDING_APPROVAL":
        assert body["policy_decision"]["outcome"] == "REQUIRES_APPROVAL"
        r = client.post(
            f"/api/v1/recovery/{opp_id}/approve",
            json={"actor": "human:integration-test"},
            headers=API_KEY,
        )
        assert r.status_code == 200, r.text
        r = client.post(
            f"/api/v1/recovery/{opp_id}/execute",
            json={"strategy_id": strategy_id, "actor": "human:integration-test"},
            headers=API_KEY,
        )
        assert r.status_code == 200, r.text
        body = r.json()
    return body


def test_full_loop_incident_to_verified_recovery(make_client, db_session):
    gateway = SimulatedPaymentGateway(success_rate=1.0)
    with make_client(gateway=gateway) as client:
        # 1. seed the demo scenario + one anchored detection pass
        r = client.post("/api/v1/demo/scenario/upi_outage_demo")
        assert r.status_code == 200, r.text
        trigger = r.json()
        assert trigger["status"] == "completed"
        assert trigger["skipped"] is False
        incident_id = trigger["incident_id"]
        assert incident_id, f"no incident detected: {trigger['detection']}"

        # 2. incidents list contains it, with filters + pagination working
        r = client.get(
            "/api/v1/incidents", params={"page_size": 50, "environment": "research"}
        )
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()["items"]]
        assert incident_id in ids
        r = client.get(
            "/api/v1/incidents", params={"severity": "CRITICAL", "environment": "research"}
        )
        assert all(i["severity"] == "CRITICAL" for i in r.json()["items"])

        # 3. detail: auto-diagnosis ran, revenue breakdown + timeline present
        r = client.get(f"/api/v1/incidents/{incident_id}")
        assert r.status_code == 200, r.text
        detail = r.json()
        assert detail["diagnosis"] is not None
        assert detail["diagnosis"]["predicted_cause"]
        assert detail["revenue"]["observed_loss"]["point_paise"] is not None
        assert detail["revenue"]["currency"] == "INR"
        kinds = {e["kind"] for e in detail["timeline"]}
        assert "detected" in kinds and "diagnosis" in kinds
        assert (
            detail["revenue_at_risk_paise"]
            == detail["revenue"]["observed_loss"]["point_paise"]
        )

        # 4. build opportunities (+ strategies) for the incident
        r = client.post(
            "/api/v1/recovery/opportunities/build",
            json={"incident_id": incident_id},
            headers=API_KEY,
        )
        assert r.status_code == 200, r.text
        built = r.json()
        assert built["created_count"] > 0
        opps = built["opportunities"]

        # 5a. payment-link strategy: inline verification -> RECOVERED (gateway
        # success_rate=1.0 in this test, so the link pays immediately)
        link_opp = next(o for o in opps if o["amount_paise"] >= 100)
        r = client.get(f"/api/v1/recovery/{link_opp['id']}/plan")
        assert r.status_code == 200
        link_strategy = next(
            s for s in r.json()["strategies"] if s["action_type"] == "create_payment_link"
        )
        body = _approve_and_execute(client, link_opp["id"], link_strategy["id"])
        assert body["status"] == "RECOVERED", body

        # 5b. retry strategy on another opportunity: the gateway call lands in
        # VERIFYING; a signed webhook through POST /webhooks/razorpay proves
        # the recovery (real signature verification + dedup + reconciler).
        retry_opp = next(o for o in opps if o["id"] != link_opp["id"] and o["payment_id"])
        r = client.get(f"/api/v1/recovery/{retry_opp['id']}/plan")
        retry_strategy = next(
            s for s in r.json()["strategies"] if s["action_type"] == "retry_payment"
        )
        body = _approve_and_execute(client, retry_opp["id"], retry_strategy["id"])
        assert body["status"] in ("VERIFYING", "RECOVERED"), body

        if body["status"] == "VERIFYING":
            payment = db_session.get(Payment, retry_opp["payment_id"])
            entity = {
                "id": payment.gateway_payment_id,
                "entity": "payment",
                "amount": payment.amount_paise,
                "currency": "INR",
                "status": "captured",
                "captured": True,
                "method": payment.method,
            }
            payload, signature, event_id = gateway.build_event("payment.captured", entity)
            r = client.post(
                "/webhooks/razorpay",
                content=payload,
                headers={
                    "X-Razorpay-Signature": signature,
                    "X-Razorpay-Event-Id": event_id,
                    "Content-Type": "application/json",
                },
            )
            assert r.status_code == 200, r.text
            assert r.json()["processed"] is True
            action = db_session.get(RecoveryAction, body["action_id"])
            assert action.status is RecoveryStatus.RECOVERED

            # duplicate delivery is acked with zero side effects
            r = client.post(
                "/webhooks/razorpay",
                content=payload,
                headers={
                    "X-Razorpay-Signature": signature,
                    "X-Razorpay-Event-Id": event_id,
                    "Content-Type": "application/json",
                },
            )
            assert r.status_code == 200
            assert r.json()["duplicate"] is True

        # 6. recovered actions are audited and the dashboard measures them
        n_recovered = db_session.scalar(
            sa.select(sa.func.count())
            .select_from(RecoveryAction)
            .where(RecoveryAction.status == RecoveryStatus.RECOVERED)
        )
        assert n_recovered >= 2
        audit_actions = {
            row.action
            for row in db_session.scalars(
                sa.select(AuditLog).where(AuditLog.entity_type == "recovery_action")
            )
        }
        assert "recovery.action.recovered" in audit_actions

        r = client.get("/api/v1/dashboard/summary", params={"environment": "research"})
        assert r.status_code == 200
        summary = r.json()
        assert summary["recovered_revenue_paise"] > 0
        assert summary["recovery_rate"] > 0
        assert summary["open_incidents"] >= 1
        assert summary["payments_observed"] > 0
        assert summary["payments_baseline_success_rate"] is not None
        assert summary["recent_incidents"]

        # 7. timeseries reflect the seeded events
        r = client.get(
            "/api/v1/dashboard/timeseries",
            params={
                "metric": "payment_success_rate",
                "granularity": "hour",
                "window_hours": 48,
                "environment": "research",
            },
        )
        assert r.status_code == 200
        assert len(r.json()["points"]) > 0
        r = client.get(
            "/api/v1/dashboard/timeseries",
            params={"metric": "recovered_revenue_paise", "environment": "research"},
        )
        assert r.status_code == 200

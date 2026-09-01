"""API-level tests for /api/v1/recovery: listing, detail (+audit refs), plan,
build, the approve->execute loop, and webhook-driven verification end to end.
"""

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import app.models as models
from app.api.deps import get_gateway_dependency
from app.db import get_db
from app.main import create_app
from app.ports import RecoveryStatus

API_KEY = {"X-API-Key": "dev-key"}


@pytest.fixture()
def api_client(db_session, sim_gateway):
    app = create_app()

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gateway_dependency] = lambda: sim_gateway
    with TestClient(app) as c:
        yield c


class TestList:
    def test_filters_and_pagination(
        self, api_client, db_session, make_opportunity, make_incident
    ):
        incident = make_incident()
        make_opportunity(incident=incident)
        make_opportunity(incident=incident)
        make_opportunity()  # different incident

        resp = api_client.get(
            "/api/v1/recovery/opportunities", params={"environment": "research"}
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 3

        resp = api_client.get(
            "/api/v1/recovery/opportunities",
            params={"incident_id": incident.id, "environment": "research"},
        )
        assert resp.json()["total"] == 2

        resp = api_client.get(
            "/api/v1/recovery/opportunities",
            params={"status": "PROPOSED", "environment": "research"},
        )
        assert resp.json()["total"] == 3
        resp = api_client.get(
            "/api/v1/recovery/opportunities",
            params={"status": "RECOVERED", "environment": "research"},
        )
        assert resp.json()["total"] == 0

        resp = api_client.get(
            "/api/v1/recovery/opportunities",
            params={"page": 2, "page_size": 2, "environment": "research"},
        )
        body = resp.json()
        assert body["page"] == 2
        assert len(body["items"]) == 1
        assert body["total"] == 3


class TestBuild:
    def test_build_is_idempotent_and_generates_strategies(
        self, api_client, db_session, windowed_incident, failed_payment
    ):
        incident = windowed_incident()
        failed_payment()
        db_session.commit()

        resp = api_client.post(
            "/api/v1/recovery/opportunities/build",
            json={"incident_id": incident.id},
            headers=API_KEY,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["created_count"] == 1
        assert body["opportunities"][0]["expected_recovery_paise"] > 0

        resp = api_client.post(
            "/api/v1/recovery/opportunities/build",
            json={"incident_id": incident.id},
            headers=API_KEY,
        )
        assert resp.json()["created_count"] == 0
        assert resp.json()["existing_count"] == 1

    def test_build_unknown_incident_404(self, api_client):
        resp = api_client.post(
            "/api/v1/recovery/opportunities/build",
            json={"incident_id": "inc_missing"},
            headers=API_KEY,
        )
        assert resp.status_code == 404


class TestPlan:
    def test_plan_returns_comparison_with_recommendation(
        self, api_client, db_session, make_opportunity, make_diagnosis, failed_payment
    ):
        opp = make_opportunity(payment=failed_payment())
        make_diagnosis(db_session.get(models.Incident, opp.incident_id), confidence=0.95)
        db_session.commit()

        resp = api_client.get(f"/api/v1/recovery/{opp.id}/plan")
        assert resp.status_code == 200
        body = resp.json()

        assert len(body["strategies"]) == 6
        recommended = next(s for s in body["strategies"] if s["selected"])
        assert body["recommended_strategy_id"] == recommended["id"]
        assert recommended["action_type"] == "retry_payment"
        assert recommended["expected_recovery_paise"] >= max(
            s["expected_recovery_paise"] for s in body["strategies"]
        )
        # the policy preview shows what the deterministic gate would say now
        assert body["policy_preview"]["outcome"] == "ALLOWED"
        # delayed retry present as retry_payment + delay_seconds
        delayed = [
            s
            for s in body["strategies"]
            if s["action_type"] == "retry_payment" and s["constraints"].get("delay_seconds")
        ]
        assert len(delayed) == 1

    def test_plan_is_stable_on_repeat(self, api_client, db_session, make_opportunity):
        opp = make_opportunity()
        db_session.commit()
        first = api_client.get(f"/api/v1/recovery/{opp.id}/plan").json()
        second = api_client.get(f"/api/v1/recovery/{opp.id}/plan").json()
        assert [s["id"] for s in first["strategies"]] == [
            s["id"] for s in second["strategies"]
        ]

    def test_plan_persists_strategies_and_policy_preview(
        self, api_client, db_session, make_opportunity
    ):
        """REGRESSION: get_plan used to flush-only, so its strategies and the
        plan-preview policy decision rolled back with the request session. The
        endpoint commits — the rows survive a rollback."""
        opp = make_opportunity()
        resp = api_client.get(f"/api/v1/recovery/{opp.id}/plan")
        assert resp.status_code == 200

        db_session.rollback()  # discards anything left uncommitted
        strategies = db_session.scalars(
            sa.select(models.RecoveryStrategy).where(
                models.RecoveryStrategy.opportunity_id == opp.id
            )
        ).all()
        assert len(strategies) == 6
        decision = db_session.scalar(
            sa.select(models.PolicyDecisionRecord).where(
                models.PolicyDecisionRecord.actor == "system:plan_preview"
            )
        )
        assert decision is not None
        assert decision.action_id is None  # preview: no action link


class TestDetail:
    def test_detail_includes_policy_decision_and_audit_refs(
        self, api_client, db_session, make_opportunity, make_diagnosis, failed_payment
    ):
        opp = make_opportunity(payment=failed_payment())
        make_diagnosis(db_session.get(models.Incident, opp.incident_id), confidence=0.95)
        db_session.commit()

        exec_resp = api_client.post(
            f"/api/v1/recovery/{opp.id}/execute",
            json={"actor": "human:console"},
            headers={**API_KEY, "X-Request-ID": "req-detail-1"},
        )
        assert exec_resp.status_code == 200

        resp = api_client.get(f"/api/v1/recovery/{opp.id}")
        assert resp.status_code == 200
        body = resp.json()

        assert body["id"] == opp.id
        (action,) = body["actions"]
        assert action["status"] == "VERIFYING"  # retry fired; awaiting truth
        assert action["policy_decision"]["outcome"] == "ALLOWED"
        assert action["policy_decision"]["policy_version"]
        audit_actions = [row["action"] for row in body["audit"]]
        assert "recovery.action.proposed" in audit_actions
        assert "recovery.action.executing" in audit_actions
        assert all(row["request_id"] == "req-detail-1" for row in body["audit"])

    def test_detail_404(self, api_client):
        assert api_client.get("/api/v1/recovery/opp_missing").status_code == 404


class TestApproveExecuteLoop:
    def test_full_loop_with_webhook_verification(
        self, api_client, db_session, sim_gateway, make_opportunity, failed_payment
    ):
        # no diagnosis -> confidence 0.784 -> approval lane
        payment = failed_payment(gateway_payment_id="pay_hook1")
        opp = make_opportunity(payment=payment)
        db_session.commit()

        r1 = api_client.post(
            f"/api/v1/recovery/{opp.id}/execute",
            json={"actor": "human:console"},
            headers=API_KEY,
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "PENDING_APPROVAL"

        r2 = api_client.post(
            f"/api/v1/recovery/{opp.id}/execute",
            json={"actor": "human:console"},
            headers=API_KEY,
        )
        assert r2.status_code == 409  # refused until approve

        r3 = api_client.post(
            f"/api/v1/recovery/{opp.id}/approve",
            json={"actor": "human:ops", "note": "reviewed"},
            headers=API_KEY,
        )
        assert r3.status_code == 200
        assert r3.json()["status"] == "APPROVED"

        r4 = api_client.post(
            f"/api/v1/recovery/{opp.id}/execute",
            json={"actor": "human:ops"},
            headers=API_KEY,
        )
        assert r4.status_code == 200
        assert r4.json()["status"] == "VERIFYING"
        assert len(sim_gateway.orders) == 1

        # webhook: the original payment captures -> verification proves recovery
        body, signature, event_id = sim_gateway.build_event(
            "payment.captured",
            {
                "id": "pay_hook1",
                "entity": "payment",
                "status": "captured",
                "amount": opp.amount_paise,
                "currency": "INR",
            },
        )
        wh = api_client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "X-Razorpay-Signature": signature,
                "X-Razorpay-Event-Id": event_id,
            },
        )
        assert wh.status_code == 200

        detail = api_client.get(f"/api/v1/recovery/{opp.id}").json()
        assert detail["status"] == "RECOVERED"
        assert detail["actions"][0]["status"] == "RECOVERED"
        assert detail["actions"][0]["verified_at"] is not None

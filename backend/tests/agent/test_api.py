"""End-to-end API tests for the agent investigation endpoints, plus service
persistence/audit behavior. Uses the shared TestClient with the in-memory DB
override from the root conftest."""

import sqlalchemy as sa

from app.models import AgentReport, AuditLog, Diagnosis
from app.services.agent.service import AgentService

API_KEY = {"X-API-Key": "dev-key"}


# -- service level -----------------------------------------------------------------


def test_service_persists_report_and_audit(db_session, agent_seed, tmp_path):
    incident = agent_seed["incident"]
    service = AgentService(db_session, artifacts_dir=tmp_path)
    row = service.investigate(incident.id)
    assert row.status == "completed"
    assert row.agent_name == "investigator"
    assert row.report_type == "investigation"
    assert row.model == "heuristic"
    assert row.duration_ms is not None
    out = row.output
    assert out["observed_facts"], "report carries observed facts"
    assert out["recommended_next_step"]["policy_preview"]["outcome"]
    # diagnosis was run as part of the investigation
    diag = db_session.scalars(
        sa.select(Diagnosis).where(Diagnosis.incident_id == incident.id)
    ).first()
    assert diag is not None
    assert out["diagnosis"]["diagnosis_id"] == diag.id
    # the run is audited
    entry = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.entity_type == "agent_report", AuditLog.entity_id == row.id
        )
    ).first()
    assert entry is not None
    assert entry.action == "agent.investigate"
    assert entry.actor == "agent:investigator"
    assert entry.details["incident_id"] == incident.id


def test_service_is_idempotent_without_force_refresh(db_session, agent_seed, tmp_path):
    incident = agent_seed["incident"]
    service = AgentService(db_session, artifacts_dir=tmp_path)
    first = service.investigate(incident.id)
    second = service.investigate(incident.id)
    assert first.id == second.id
    third = service.investigate(incident.id, force_refresh=True)
    assert third.id != first.id
    # latest() points at the fresh report
    assert service.latest(incident.id).id == third.id


def test_service_marks_llm_fallback_reports(db_session, agent_seed, tmp_path):
    """reasoner_factory injection: a broken LLM reasoner yields a degraded
    heuristic-fallback report, still persisted and completed."""
    from app.services.agent.reasoners import LlmReasoner

    def bad_chat(messages, specs):
        return {
            "choices": [{"message": {"role": "assistant", "content": "nope"}}],
            "usage": {"total_tokens": 1},
        }

    def factory(tools):
        return LlmReasoner(tools, api_key="k", model="m", chat_fn=bad_chat)

    service = AgentService(db_session, artifacts_dir=tmp_path, reasoner_factory=factory)
    row = service.investigate(agent_seed["incident"].id)
    assert row.status == "completed"
    assert row.output["degraded"] is True
    assert "fallback: heuristic" in row.output["generated_by"]


# -- API level -----------------------------------------------------------------


def test_investigate_endpoint_e2e(client, db_session, agent_seed):
    incident = agent_seed["incident"]
    r = client.post(f"/api/v1/incidents/{incident.id}/investigate", json={}, headers=API_KEY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["incident_id"] == incident.id
    report = body["report"]
    # the clean three-way separation the frontend renders
    assert report["observed_facts"]
    assert report["ai_inferences"]
    assert report["recommended_actions"]
    for fact in report["observed_facts"]:
        assert fact["tool"] and fact["evidence_ids"]
    for inf in report["ai_inferences"]:
        assert 0.0 <= inf["confidence"] <= 1.0
    assert report["recommended_next_step"]["policy_preview"]["outcome"] in {
        "ALLOWED",
        "BLOCKED",
        "REQUIRES_APPROVAL",
    }
    assert report["generated_by"] == "heuristic"
    # persisted
    row = db_session.get(AgentReport, body["report_id"])
    assert row is not None and row.status == "completed"


def test_get_investigation_returns_latest(client, agent_seed):
    incident = agent_seed["incident"]
    r1 = client.post(f"/api/v1/incidents/{incident.id}/investigate", json={}, headers=API_KEY)
    assert r1.status_code == 200
    r2 = client.get(f"/api/v1/incidents/{incident.id}/investigation")
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["report_id"]
    assert r2.json()["summary"] == r1.json()["report"]["summary"]


def test_investigate_requires_api_key(client, agent_seed):
    r = client.post(f"/api/v1/incidents/{agent_seed['incident'].id}/investigate", json={})
    assert r.status_code == 401


def test_investigate_unknown_incident_404(client):
    r = client.post("/api/v1/incidents/inc_missing/investigate", json={}, headers=API_KEY)
    assert r.status_code == 404
    r2 = client.get("/api/v1/incidents/inc_missing/investigation")
    assert r2.status_code == 404


def test_investigation_on_empty_evidence_escalates(client, empty_incident):
    r = client.post(
        f"/api/v1/incidents/{empty_incident.id}/investigate", json={}, headers=API_KEY
    )
    assert r.status_code == 200
    report = r.json()["report"]
    assert report["escalated"] is True
    assert report["recommended_next_step"]["action_type"] == "escalate_human"

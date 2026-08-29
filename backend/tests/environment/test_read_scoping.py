"""Environment isolation (b): incidents, recovery opportunities, and the audit
trail are scoped identically — a real_test query never returns research rows
and a research query never returns real_test rows. Detail-by-id endpoints
stay consistent with the row's own environment (no scope param)."""

import sqlalchemy as sa

import app.models as models
from app.db import utcnow
from app.ports import ActionType, RecoveryStatus


def _audit(db_session, *, environment: str, entity_id: str) -> models.AuditLog:
    row = models.AuditLog(
        entity_type="incident",
        entity_id=entity_id,
        actor="agent:test",
        action="test.row",
        details={},
        created_at=utcnow(),
        environment=environment,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_incidents_list_scoped_by_environment(client, make_incident):
    research_inc = make_incident(title="research incident", environment="research")
    real_inc = make_incident(title="real_test incident", environment="real_test")

    body = client.get("/api/v1/incidents").json()  # default real_test
    assert [i["id"] for i in body["items"]] == [real_inc.id]
    assert body["total"] == 1
    assert body["items"][0]["environment"] == "real_test"

    body = client.get("/api/v1/incidents", params={"environment": "research"}).json()
    assert [i["id"] for i in body["items"]] == [research_inc.id]
    assert body["total"] == 1
    assert body["items"][0]["environment"] == "research"

    # filters compose with the environment scope
    body = client.get(
        "/api/v1/incidents",
        params={"environment": "research", "severity": "CRITICAL"},
    ).json()
    assert body["total"] == 0  # the research incident is MEDIUM by default


def test_incident_detail_follows_the_rows_own_environment(client, make_incident):
    research_inc = make_incident(environment="research")
    r = client.get(f"/api/v1/incidents/{research_inc.id}")
    assert r.status_code == 200
    assert r.json()["environment"] == "research"


def test_opportunities_list_scoped_by_environment(client, make_opportunity):
    research_opp = make_opportunity(environment="research")
    real_opp = make_opportunity(environment="real_test")

    body = client.get("/api/v1/recovery/opportunities").json()  # default real_test
    assert [o["id"] for o in body["items"]] == [real_opp.id]
    assert body["items"][0]["environment"] == "real_test"

    body = client.get(
        "/api/v1/recovery/opportunities", params={"environment": "research"}
    ).json()
    assert [o["id"] for o in body["items"]] == [research_opp.id]
    assert body["items"][0]["environment"] == "research"

    # detail/plan stay consistent with the opportunity's own environment
    r = client.get(f"/api/v1/recovery/{research_opp.id}")
    assert r.status_code == 200
    assert r.json()["environment"] == "research"
    r = client.get(f"/api/v1/recovery/{research_opp.id}/plan")
    assert r.status_code == 200
    assert r.json()["opportunity_id"] == research_opp.id


def test_audit_list_scoped_by_environment(client, db_session):
    research_row = _audit(db_session, environment="research", entity_id="inc_r")
    real_row = _audit(db_session, environment="real_test", entity_id="inc_t")

    body = client.get("/api/v1/audit").json()  # default real_test
    assert [a["id"] for a in body["items"]] == [real_row.id]
    assert body["items"][0]["environment"] == "real_test"

    body = client.get("/api/v1/audit", params={"environment": "research"}).json()
    assert [a["id"] for a in body["items"]] == [research_row.id]
    assert body["items"][0]["environment"] == "research"

    # entity filters compose with the scope
    body = client.get(
        "/api/v1/audit",
        params={"environment": "research", "entity_id": "inc_t"},
    ).json()
    assert body["total"] == 0


def test_writer_stamping_chain(db_session, make_incident, make_opportunity):
    """The environment stamp propagates incident -> opportunity -> action, and
    ORM defaults stay 'research' (the safe failure direction)."""
    incident = make_incident(environment="research")
    assert incident.environment == "research"

    opp = make_opportunity(incident=incident, environment="research")
    action = models.RecoveryAction(
        opportunity_id=opp.id,
        incident_id=incident.id,
        action_type=ActionType.CREATE_PAYMENT_LINK,
        status=RecoveryStatus.PROPOSED,
        amount_paise=opp.amount_paise,
        actor="agent:test",
        proposed_at=utcnow(),
        environment=opp.environment,
    )
    db_session.add(action)
    db_session.commit()
    assert action.environment == "research"

    # ORM default: an unstamped row is honestly research, never real_test.
    bare = models.AuditLog(
        entity_type="x", entity_id="y", actor="a", action="b", created_at=utcnow()
    )
    db_session.add(bare)
    db_session.commit()
    assert bare.environment == "research"


def test_audit_reset_row_is_research_tagged(client, db_session):
    """demo.reset's own audit row can never appear in a real_test query."""
    r = client.post("/api/v1/demo/reset")
    assert r.status_code == 200
    row = db_session.get(models.AuditLog, r.json()["audit_id"])
    assert row is not None and row.action == "demo.reset"
    assert row.environment == "research"

    body = client.get("/api/v1/audit").json()  # real_test default
    assert body["total"] == 0
    body = client.get("/api/v1/audit", params={"environment": "research"}).json()
    assert any(a["action"] == "demo.reset" for a in body["items"])


def test_orm_models_expose_the_new_tables(db_session):
    """sync_runs + connection_state are registered and writable."""
    run = models.SyncRun(actor="agent:sync", entity_counts={"payments": 3})
    db_session.add(run)
    state = models.ConnectionState()
    db_session.add(state)
    db_session.commit()
    assert run.id.startswith("sr_")
    assert run.status == "running"
    assert state.id == "merchant"
    assert state.sync_enabled is True
    n = db_session.scalar(sa.select(sa.func.count()).select_from(models.SyncRun))
    assert n == 1

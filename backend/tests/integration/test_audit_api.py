"""Audit API integration: filters, pagination, newest-first ordering, empty DB.

Rows are seeded straight into `audit_logs` (the same table
app.services.policy.audit.record writes); GET /api/v1/audit is read-only.
"""

from datetime import timedelta

from app.db import utcnow
from app.models import AuditLog


def _seed(db_session) -> list[AuditLog]:
    """5 rows across 2 entity types, oldest first; returns them oldest-first."""
    base = utcnow().replace(microsecond=0) - timedelta(minutes=10)
    specs = [
        ("incident", "inc_1", "agent:detection", "incident.created"),
        ("incident", "inc_1", "human:ops", "incident.status_change"),
        ("recovery_opportunity", "opp_1", "system:builder", "opportunity.created"),
        ("recovery_action", "act_1", "human:ops", "recovery.approve"),
        ("incident", "inc_2", "agent:detection", "incident.created"),
    ]
    rows = []
    for i, (entity_type, entity_id, actor, action) in enumerate(specs):
        row = AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor,
            action=action,
            details={"seq": i},
            request_id=f"req-{i}" if i % 2 == 0 else None,
            created_at=base + timedelta(minutes=i),
        )
        db_session.add(row)
        rows.append(row)
    db_session.commit()
    return rows


def test_audit_empty_db(client):
    r = client.get("/api/v1/audit")
    assert r.status_code == 200
    body = r.json()
    assert body == {"items": [], "total": 0, "page": 1, "page_size": 50}


def test_audit_lists_all_newest_first(client, db_session):
    rows = _seed(db_session)
    r = client.get("/api/v1/audit", params={"environment": "research"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["page_size"] == 50
    items = body["items"]
    assert len(items) == 5
    # newest-first: reverse of insertion order
    assert [it["id"] for it in items] == [row.id for row in reversed(rows)]
    # full row shape (what the frontend timeline renders)
    first = items[0]
    assert first["entity_type"] == "incident"
    assert first["entity_id"] == "inc_2"
    assert first["actor"] == "agent:detection"
    assert first["action"] == "incident.created"
    assert first["details"] == {"seq": 4}
    assert first["request_id"] == "req-4"
    assert first["created_at"].startswith("20")
    assert items[1]["request_id"] is None  # odd seq rows have no request id


def test_audit_filter_by_entity_type(client, db_session):
    _seed(db_session)
    r = client.get(
        "/api/v1/audit", params={"entity_type": "incident", "environment": "research"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert {it["entity_type"] for it in body["items"]} == {"incident"}


def test_audit_filter_by_entity_id(client, db_session):
    _seed(db_session)
    r = client.get(
        "/api/v1/audit",
        params={"entity_type": "incident", "entity_id": "inc_1", "environment": "research"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert [it["action"] for it in body["items"]] == [
        "incident.status_change",
        "incident.created",
    ]


def test_audit_filter_no_match(client, db_session):
    _seed(db_session)
    r = client.get("/api/v1/audit", params={"entity_id": "nope", "environment": "research"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_audit_pagination(client, db_session):
    rows = _seed(db_session)
    r = client.get("/api/v1/audit", params={"page": 1, "page_size": 2, "environment": "research"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["page_size"] == 2
    page1_ids = [it["id"] for it in body["items"]]
    assert page1_ids == [rows[4].id, rows[3].id]

    r = client.get("/api/v1/audit", params={"page": 3, "page_size": 2, "environment": "research"})
    body = r.json()
    assert [it["id"] for it in body["items"]] == [rows[0].id]

    r = client.get("/api/v1/audit", params={"page": 4, "page_size": 2, "environment": "research"})
    assert r.json()["items"] == []


def test_audit_rejects_bad_pagination(client):
    assert client.get("/api/v1/audit", params={"page": 0}).status_code == 422
    assert client.get("/api/v1/audit", params={"page_size": 0}).status_code == 422
    assert client.get("/api/v1/audit", params={"page_size": 201}).status_code == 422


def test_audit_filter_environment_all(client, db_session):
    """Rows stamped environment='all' (unfiltered policy-backtest runs, see
    api/v1/policy.py) belong to no single environment: they are queryable via
    ?environment=all and stay out of the scoped real_test/research queries."""
    row = AuditLog(
        entity_type="policy_backtest_run",
        entity_id="pbr_1",
        actor="system:policy_backtest",
        action="policy.backtest",
        details={"environment": None},
        created_at=utcnow().replace(microsecond=0),
        environment="all",
    )
    db_session.add(row)
    db_session.commit()

    r = client.get("/api/v1/audit", params={"environment": "all"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == row.id
    assert body["items"][0]["action"] == "policy.backtest"
    assert body["items"][0]["environment"] == "all"
    # scoped queries never surface the unfiltered row
    assert client.get("/api/v1/audit").json()["total"] == 0  # real_test default
    assert (
        client.get("/api/v1/audit", params={"environment": "research"}).json()["total"] == 0
    )

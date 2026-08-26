"""Foundation smoke tests: health endpoints, error envelope, OpenAPI contract,
API-key guard, and an ORM round-trip proving the model layer works end to end."""


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"]["status"] == "ok"


def test_system_health(client):
    r = client.get("/api/v1/system/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body["checks"]) >= {"database", "policy_engine", "llm_provider", "gateway"}
    assert body["simulation_mode"] is True


def test_404_error_shape(client):
    r = client.get("/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "not_found"
    assert "request_id" in body["error"]
    assert r.headers["X-Request-ID"]


def test_openapi_served(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    for expected in [
        "/healthz",
        "/api/v1/system/health",
        "/api/v1/dashboard/summary",
        "/api/v1/incidents",
        "/api/v1/incidents/{incident_id}/investigate",
        "/api/v1/recovery/opportunities",
        "/api/v1/recovery/{opportunity_id}/execute",
        "/api/v1/audit",
        "/api/v1/evaluation/runs",
        "/api/v1/detection/run",
        "/api/v1/demo/reset",
        "/webhooks/razorpay",
    ]:
        assert expected in paths, f"missing route in openapi: {expected}"


def test_mutating_route_requires_api_key(client):
    r = client.post("/api/v1/recovery/opp_x/approve", json={"actor": "human:t"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    r2 = client.post(
        "/api/v1/recovery/opp_x/approve",
        json={"actor": "human:t"},
        headers={"X-API-Key": "dev-key"},
    )
    assert r2.status_code == 501  # stub, but authorized


def test_demo_and_detection_exempt_from_api_key(client):
    assert client.post("/api/v1/demo/reset").status_code == 501
    assert client.post("/api/v1/detection/run", json={}).status_code == 501


def test_list_endpoints_return_empty_valid_pages(client):
    for path in ["/api/v1/incidents", "/api/v1/recovery/opportunities", "/api/v1/audit"]:
        r = client.get(path)
        assert r.status_code == 200, path
        body = r.json()
        assert body["items"] == [] and body["total"] == 0


def test_orm_roundtrip(make_merchant, make_payment, make_incident, db_session):
    import sqlalchemy as sa

    import app.models as models
    from app.ports import IncidentStatus

    merchant = make_merchant()
    payment = make_payment(merchant=merchant)
    incident = make_incident(revenue_at_risk_paise=payment.amount_paise)
    assert merchant.id.startswith("mch_")
    assert payment.id.startswith("pay_")
    assert incident.id.startswith("inc_")
    assert incident.status == IncidentStatus.OPEN
    # tz-aware UTC read-back even from SQLite
    assert incident.created_at.tzinfo is not None
    n = db_session.scalar(sa.select(sa.func.count()).select_from(models.Incident))
    assert n == 1

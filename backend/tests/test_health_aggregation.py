"""Top-level health status aggregates the component checks (demo-chaos F2).

The chaos doc's recommendation is to DERIVE the top-level status from checks
(an external readiness gate reading only the top level must not see "ok" next
to a down database) — the HTTP code stays 200: readyz answering promptly with
an honest payload is the designed degradation (F1), and the UI renders the
component rows from the same body.
"""

import app.api.v1.health as health
from app.schemas.common import ComponentHealth


def test_aggregate_ok_when_all_checks_healthy():
    checks = {
        "database": ComponentHealth(status="ok"),
        # "disabled" is a deliberate configuration, not a failure.
        "llm_provider": ComponentHealth(status="disabled"),
    }
    assert health._aggregate_status(checks) == "ok"


def test_aggregate_error_when_database_down():
    checks = {
        "database": ComponentHealth(status="down", detail="OperationalError"),
        "policy_engine": ComponentHealth(status="ok"),
    }
    assert health._aggregate_status(checks) == "error"


def test_aggregate_degraded_when_non_db_check_down():
    checks = {
        "database": ComponentHealth(status="ok"),
        "policy_engine": ComponentHealth(status="down", detail="missing file"),
    }
    assert health._aggregate_status(checks) == "degraded"


def test_readyz_top_level_status_reflects_db_down(client, monkeypatch):
    monkeypatch.setattr(
        health, "_db_check", lambda db: ComponentHealth(status="down", detail="boom")
    )
    r = client.get("/readyz")
    assert r.status_code == 200  # honest payload, still prompt (see docstring)
    body = r.json()
    assert body["checks"]["database"]["status"] == "down"
    assert body["status"] == "error"


def test_system_health_top_level_status_reflects_policy_down(client, monkeypatch):
    monkeypatch.setattr(
        health, "_policy_check", lambda: ComponentHealth(status="down", detail="boom")
    )
    r = client.get("/api/v1/system/health")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["policy_engine"]["status"] == "down"
    assert body["status"] == "degraded"

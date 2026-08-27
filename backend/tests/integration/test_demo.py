"""Demo endpoints: scenario listing, idempotent scenario trigger, and reset
determinism (exactly one audit row, derived tables actually cleared)."""

import pytest
import sqlalchemy as sa

from app.models import AuditLog, Incident, Payment, SimulatorRun
from app.simulator.config import (
    SCENARIOS,
    IncidentKind,
    IncidentSpec,
    SimulatorConfig,
)


@pytest.fixture()
def tiny_scenario():
    """A minimal registered scenario (>= the simulator's 1000-event floor)."""

    def _tiny() -> SimulatorConfig:
        return SimulatorConfig(
            scenario="tiny_test",
            days=2,
            target_events=1200,
            customers=60,
            incidents=(
                IncidentSpec(
                    IncidentKind.METHOD_OUTAGE,
                    day_fraction=0.5,
                    start_hour_ist=19.0,
                    duration_hours=2.0,
                    params={"method": "upi", "fail_boost": 0.85},
                ),
            ),
        )

    SCENARIOS["tiny_test"] = ("tiny test preset", _tiny)
    try:
        yield "tiny_test"
    finally:
        SCENARIOS.pop("tiny_test", None)


def test_list_scenarios(client):
    r = client.get("/api/v1/demo/scenarios")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()["scenarios"]}
    assert {"standard", "quiet", "upi_outage_demo", "payday_wave_demo", "storm"} <= names


def test_unknown_scenario_404(client):
    r = client.post("/api/v1/demo/scenario/definitely-not-a-scenario")
    assert r.status_code == 404


def test_scenario_trigger_is_idempotent(client, db_session, tiny_scenario):
    r = client.post(f"/api/v1/demo/scenario/{tiny_scenario}")
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["status"] == "completed" and first["skipped"] is False
    assert first["simulator_run_id"]
    assert first["detection"]["status"] == "completed"
    n_runs = db_session.scalar(sa.select(sa.func.count()).select_from(SimulatorRun))
    n_payments = db_session.scalar(sa.select(sa.func.count()).select_from(Payment))

    r = client.post(f"/api/v1/demo/scenario/{tiny_scenario}")
    second = r.json()
    assert second["skipped"] is True
    assert second["simulator_run_id"] == first["simulator_run_id"]
    # nothing re-seeded, and detection upserted rather than duplicating
    assert db_session.scalar(sa.select(sa.func.count()).select_from(SimulatorRun)) == n_runs
    assert db_session.scalar(sa.select(sa.func.count()).select_from(Payment)) == n_payments
    assert second["detection"]["incidents_created"] == []


def test_reset_clears_derived_data_and_writes_one_audit_row(client, db_session, tiny_scenario):
    client.post(f"/api/v1/demo/scenario/{tiny_scenario}")
    audits_before = db_session.scalar(sa.select(sa.func.count()).select_from(AuditLog))

    r = client.post("/api/v1/demo/reset")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["cleared"]["payments"] > 0
    assert body["cleared"]["simulator_runs"] == 1
    assert "evaluation_runs" in body["kept"]
    assert body["audit_id"]

    assert db_session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 0
    assert db_session.scalar(sa.select(sa.func.count()).select_from(Incident)) == 0
    assert db_session.scalar(sa.select(sa.func.count()).select_from(SimulatorRun)) == 0
    audits_after = db_session.scalar(sa.select(sa.func.count()).select_from(AuditLog))
    assert audits_after - audits_before == 1
    row = db_session.scalars(
        sa.select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(1)
    ).first()
    assert row.action == "demo.reset"
    assert row.details["cleared"]["payments"] > 0

    # reset on an empty environment is a clean no-op
    r = client.post("/api/v1/demo/reset")
    assert r.status_code == 200
    assert all(v == 0 for v in r.json()["cleared"].values())

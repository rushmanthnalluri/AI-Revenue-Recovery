"""Evaluation API + harness invariants: stored-row reads, synchronous run,
and the safety contract (zero unsafe actions, zero refunds, every execution
gated or human-approved)."""

import sqlalchemy as sa

from app.models import EvaluationRun, Experiment
from app.services.evaluation import EvaluationRunner

API_KEY = {"X-API-Key": "dev-key"}

_TINY = {"days": 2, "events": 1200, "customers": 60}


def test_evaluation_run_end_to_end_via_api(client, db_session):
    r = client.post(
        "/api/v1/evaluation/run",
        json={"name": "it-eval", "scenario": "upi_outage_demo", **_TINY},
        headers=API_KEY,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["experiment_id"]

    metrics = body["metrics"]
    for arm in ("baseline", "pulsecover"):
        assert metrics["arms"][arm]["simulator_run_id"]
    # Safety contract: no ungated execution, ever.
    assert metrics["unsafe_action_count"] == 0
    # The baseline retried every failed payment; PulseCover intervened less.
    comparison = metrics["comparison"]
    assert comparison["interventions_baseline"] >= comparison["interventions_pulserecover"]

    # stored rows
    run = db_session.get(EvaluationRun, body["run_id"])
    assert run is not None and run.status == "completed"
    assert db_session.scalar(
        sa.select(sa.func.count()).select_from(Experiment).where(Experiment.status == "completed")
    ) >= 1

    r = client.get("/api/v1/evaluation/runs")
    assert r.status_code == 200
    assert r.json()["total"] == 1
    r = client.get(f"/api/v1/evaluation/runs/{body['run_id']}")
    assert r.status_code == 200
    assert r.json()["metrics"]["comparison"] == comparison
    r = client.get("/api/v1/evaluation/metrics")
    assert r.status_code == 200
    agg = r.json()
    assert agg["runs_count"] == 1
    assert agg["unsafe_action_count"] == 0
    assert agg["baseline_recovered_revenue_paise"] >= 0

    r = client.get("/api/v1/evaluation/runs/run_does_not_exist")
    assert r.status_code == 404
    r = client.post(
        "/api/v1/evaluation/run", json={"scenario": "nope"}, headers=API_KEY
    )
    assert r.status_code == 400


def test_runner_metrics_are_deterministic_per_seed(db_session):
    kwargs = dict(name="det", scenario="upi_outage_demo", seed=7, days=2, events=1200, customers=60)
    first = EvaluationRunner(db_session).run(**kwargs)
    second = EvaluationRunner(db_session).run(**kwargs)
    assert first.status == second.status == "completed"
    a, b = first.metrics["arms"], second.metrics["arms"]
    assert a["baseline"]["recovered_revenue_paise"] == b["baseline"]["recovered_revenue_paise"]
    assert a["pulsecover"]["interventions_count"] == b["pulsecover"]["interventions_count"]

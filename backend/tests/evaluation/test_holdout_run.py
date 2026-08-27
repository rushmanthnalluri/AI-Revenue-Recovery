"""End-to-end holdout behavior: metrics shape, determinism per seed,
treatment/holdout isolation (zero opportunities AND zero actions for
held-out customers), the zero-unsafe invariant, and the additive API
surface. Runs at the tiny plumbing scale (detection is not under test
here — it has its own suite)."""

import json

import sqlalchemy as sa

from app.models import EvaluationRun
from app.services.evaluation import EvaluationRunner

API_KEY = {"X-API-Key": "dev-key"}

_TINY = {"days": 2, "events": 1200, "customers": 60}
_RUN = dict(name="holdout-it", scenario="upi_outage_demo", seed=11, **_TINY)


def test_holdout_metrics_shape_and_isolation(db_session):
    run = EvaluationRunner(db_session).run(**_RUN, holdout_fraction=0.25)
    assert run.status == "completed"
    holdout = run.metrics["holdout"]

    # Groups partition the pre-registered denominator: ALL first-attempt
    # failed payments in the arm.
    total = run.metrics["arms"]["pulsecover"]["failed_payments_count"]
    t = holdout["treatment"]
    h = holdout["holdout"]
    assert t["failed_payments"] + h["failed_payments"] == total
    assert holdout["customers"]["treatment"] + holdout["customers"]["holdout"] > 0

    # Isolation: absolutely nothing built or executed for held-out customers.
    assert holdout["isolation"]["holdout_opportunities_count"] == 0
    assert holdout["isolation"]["holdout_actions_count"] == 0
    # The safety invariant is preserved with the holdout active.
    assert run.metrics["unsafe_action_count"] == 0

    # Both groups share the organic baseline; treatment adds the loop's
    # actions. The recovery breakdown is internally consistent.
    assert t["recovered_via_action"] + t["recovered_organic"] == t["recovered_payments"]
    assert t["recovered_via_action"] <= run.metrics["arms"]["pulsecover"]["recovered_actions_count"]

    # The estimand: point + CI, always bracketed, never a bare estimate.
    lift = holdout["lift"]
    point = t["recovery_rate"] - h["recovery_rate"]
    assert lift["point"] == round(point, 6)
    assert lift["ci95_low"] <= lift["point"] <= lift["ci95_high"]
    assert lift["ci95_high"] > lift["ci95_low"]
    assert -1.0 <= lift["ci95_low"] and lift["ci95_high"] <= 1.0

    # Attribution window + strata are persisted for the stored-row reads.
    assert holdout["attribution_window"]["max_window_hours"] > 0
    by_class = holdout["strata"]["by_failure_class"]
    assert by_class
    assert sum(r["treatment"]["failed_payments"] for r in by_class) == t["failed_payments"]
    assert sum(r["holdout"]["failed_payments"] for r in by_class) == h["failed_payments"]
    by_method = holdout["strata"]["by_method"]
    assert sum(r["treatment"]["failed_payments"] for r in by_method) == t["failed_payments"]

    # Mix-adjusted secondary estimator: present, bracketed, bounded.
    adj = holdout["lift_class_adjusted"]
    assert adj["ci95_low"] <= adj["point"] <= adj["ci95_high"]
    assert -1.0 <= adj["ci95_low"] and adj["ci95_high"] <= 1.0

    # Realized membership tracks the configured fraction (fixed seed → the
    # realized share is deterministic; the tolerance only guards regressions).
    assert abs(holdout["realized_fraction"] - 0.25) < 0.15
    assert holdout["configured_fraction"] == 0.25


def _strip_wallclock(metrics: dict) -> dict:
    """Wall-clock pipeline latency (MTTR) is an operational measurement and
    legitimately varies run to run; every simulator-derived metric must be
    bit-identical between identical-seed runs."""
    m = dict(metrics)
    m.pop("mean_time_to_recover_minutes", None)
    arms = dict(m["arms"])
    pulsecover = dict(arms["pulsecover"])
    pulsecover.pop("mttr_minutes", None)
    arms["pulsecover"] = pulsecover
    m["arms"] = arms
    return m


def test_holdout_membership_and_metrics_are_deterministic_per_seed(db_session):
    first = EvaluationRunner(db_session).run(**_RUN, holdout_fraction=0.25)
    second = EvaluationRunner(db_session).run(**_RUN, holdout_fraction=0.25)
    assert first.status == second.status == "completed"
    a, b = first.metrics["holdout"], second.metrics["holdout"]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert first.metrics["incremental_lift"] == second.metrics["incremental_lift"]
    # Full action-layer determinism: detection ids, opportunities, executed
    # actions, conversion draws, recovered revenue — everything except
    # wall-clock MTTR reproduces exactly.
    assert json.dumps(_strip_wallclock(first.metrics), sort_keys=True) == json.dumps(
        _strip_wallclock(second.metrics), sort_keys=True
    )


def test_zero_fraction_disables_the_holdout(db_session):
    run = EvaluationRunner(db_session).run(**_RUN, holdout_fraction=0.0)
    assert run.status == "completed"
    assert "holdout" not in run.metrics
    assert "incremental_lift" not in run.metrics


def test_api_run_with_holdout_fraction_and_stored_reads(client, db_session):
    r = client.post(
        "/api/v1/evaluation/run",
        json={"name": "holdout-api", "scenario": "upi_outage_demo", "seed": 11,
              "holdout_fraction": 0.25, **_TINY},
        headers=API_KEY,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    holdout = body["metrics"]["holdout"]
    assert holdout["isolation"]["holdout_actions_count"] == 0
    assert body["metrics"]["incremental_lift"] == holdout["lift"]["point"]

    # GET endpoints serve the stored rows only.
    run = db_session.get(EvaluationRun, body["run_id"])
    assert run is not None and run.metrics["holdout"]["lift"] == holdout["lift"]
    r = client.get(f"/api/v1/evaluation/runs/{body['run_id']}")
    assert r.status_code == 200
    assert r.json()["metrics"]["holdout"]["strata"]["by_failure_class"]
    r = client.get("/api/v1/evaluation/metrics")
    assert r.status_code == 200
    agg = r.json()
    assert agg["runs_count"] == 1
    assert agg["incremental_lift"] == holdout["lift"]["point"]


def test_api_rejects_invalid_holdout_fraction(client):
    for bad in (1.0, 1.5, -0.1):
        r = client.post(
            "/api/v1/evaluation/run",
            json={"scenario": "upi_outage_demo", "holdout_fraction": bad, **_TINY},
            headers=API_KEY,
        )
        assert r.status_code == 400, (bad, r.text)


def test_validation_failure_persists_no_rows(client, db_session):
    r = client.post(
        "/api/v1/evaluation/run",
        json={"scenario": "upi_outage_demo", "holdout_fraction": 2.0, **_TINY},
        headers=API_KEY,
    )
    assert r.status_code == 400
    count = db_session.scalar(sa.select(sa.func.count()).select_from(EvaluationRun))
    assert count == 0

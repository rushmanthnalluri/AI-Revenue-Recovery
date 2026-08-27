"""Demo-scenario integration tests: terminal states + determinism.

Each scenario runs TWICE against two fresh scratch DBs (module-scoped fixture;
seeding is the expensive part) using the exact configs the CLI demo uses:

- terminal-state tests assert each scenario's closed loop lands where the
  state machine says it must (RECOVERED / PENDING_APPROVAL -> RECOVERED /
  UNKNOWN -> RECOVERED / REJECTED with zero gateway calls);
- the determinism test asserts the two runs produce identical key numbers
  (ids and wall-clock timestamps are projected out — entity ids are uuid4 by
  design, see app/ids.py).

Nothing here mocks the pipeline: real simulator seed, real FastAPI routers via
TestClient, real policy file, real signed simulator webhooks.

Run: pytest tests/demo -v   (slow by design — ten seeded end-to-end runs)
"""

import pytest

from scripts.demo_run import run_scenario

SCENARIO_NAMES = ["A", "B", "C", "D", "E"]

RUPEES = 100
AUTO_CEILING_PAISE = 5_000 * RUPEES
SCENARIO_A_RISK_BAR_PAISE = 800_000 * RUPEES  # Rs 8 lakh


@pytest.fixture(scope="module")
def scenario_runs(tmp_path_factory):
    """Two fresh runs per scenario: {name: (run1_result, run2_result)}."""
    runs = {}
    for name in SCENARIO_NAMES:
        workdir = tmp_path_factory.mktemp(f"demo_{name}")
        run1 = run_scenario(name, workdir / "run1.db")
        run2 = run_scenario(name, workdir / "run2.db")
        runs[name] = (run1, run2)
    return runs


def _projection(result: dict) -> dict:
    """Key numbers only: strip uuid4 entity ids (deliberately non-deterministic)
    and keep every number, status, label, and rule the demo prints."""

    def _detection(d: dict) -> dict:
        return {
            "baseline_value": d["baseline_value"],
            "observed_value": d["observed_value"],
            "deviation_pct": d["deviation_pct"],
            "severity": d["severity"],
            "affected_payments_count": d["affected_payments_count"],
            "revenue_at_risk_paise": d["revenue_at_risk_paise"],
            "ground_truth": d["ground_truth"],
        }

    def _execution(e: dict) -> dict:
        return {
            "lane": e["lane"],
            "amount_paise": e["amount_paise"],
            "failure_class": e["failure_class"],
            "recommended_action_type": e["recommended_action_type"],
            "confidence": e["confidence"],
            "policy_outcome": e["policy_outcome"],
            "rules_matched": e["rules_matched"],
            "approved_by_human": e["approved_by_human"],
            "webhook_status": e["webhook_status"],
            "final_status": e["final_status"],
            "recovered_paise": e["recovered_paise"],
        }

    projected = {
        "scenario": result["scenario"],
        "seed_run_id": result["seed_run_id"],  # config-derived, deterministic
        "detection": _detection(result["detection"]),
        "opportunities_created": result["opportunities_created"],
        "gateway_mutation_attempts": result["gateway_mutation_attempts"],
    }
    if result.get("diagnosis"):
        projected["diagnosis"] = {
            "label": result["diagnosis"]["label"],
            "confidence": result["diagnosis"]["confidence"],
            "model_name": result["diagnosis"]["model_name"],
        }
    for key in ("executions",):
        if key in result:
            projected[key] = [_execution(e) for e in result[key]]
    for key in (
        "recovered_total_paise",
        "status_after_timeout",
        "status_after_requery",
        "final_status",
        "amount_paise",
        "forced_action_type",
        "policy_outcome",
        "rules_matched",
        "audit_rows_for_action",
        "audit_rows_sampled",
    ):
        if key in result:
            projected[key] = result[key]
    return projected


# ---------------------------------------------------------------------------
# terminal states
# ---------------------------------------------------------------------------


def test_scenario_a_major_degradation_closed_loop(scenario_runs):
    (run, _) = scenario_runs["A"]
    det = run["detection"]
    # The tuned drop the hiring panel sees (real measured numbers).
    assert det["baseline_value"] > det["observed_value"]
    assert det["observed_value"] < 0.80  # a double-digit-point drop
    assert det["revenue_at_risk_paise"] >= SCENARIO_A_RISK_BAR_PAISE
    assert run["opportunities_created"] > 0
    lanes = [e["lane"] for e in run["executions"]]
    assert lanes == ["approval", "auto", "auto"]
    for execution in run["executions"]:
        assert execution["final_status"] == "RECOVERED"
        assert execution["webhook_status"] == "received"
        assert execution["recovered_paise"] == execution["amount_paise"]
    approval = run["executions"][0]
    assert approval["policy_outcome"] == "REQUIRES_APPROVAL"
    assert approval["approved_by_human"] is True
    assert approval["amount_paise"] > AUTO_CEILING_PAISE
    for execution in run["executions"][1:]:
        assert execution["policy_outcome"] == "ALLOWED"
        assert execution["approved_by_human"] is False
        assert execution["amount_paise"] <= AUTO_CEILING_PAISE
        # Auto-execute must be genuinely earned, not asserted: the strategy
        # confidence clears the policy floor on the real diagnosis output.
        assert execution["confidence"] >= 0.85
    assert run["recovered_total_paise"] == sum(
        e["amount_paise"] for e in run["executions"]
    )
    # One gateway mutation per execution, never more.
    assert run["gateway_mutation_attempts"] == len(run["executions"])


def test_scenario_b_safe_autonomous_recovery(scenario_runs):
    (run, _) = scenario_runs["B"]
    (execution,) = run["executions"]
    assert execution["policy_outcome"] == "ALLOWED"
    assert execution["approved_by_human"] is False
    assert execution["amount_paise"] <= AUTO_CEILING_PAISE
    assert execution["confidence"] >= 0.85
    assert execution["final_status"] == "RECOVERED"
    assert execution["webhook_status"] == "received"
    assert run["recovered_total_paise"] == execution["amount_paise"]
    assert run["gateway_mutation_attempts"] == 1


def test_scenario_c_human_approval_lane(scenario_runs):
    (run, _) = scenario_runs["C"]
    (execution,) = run["executions"]
    assert execution["amount_paise"] > AUTO_CEILING_PAISE
    assert execution["policy_outcome"] == "REQUIRES_APPROVAL"
    assert "approval.amount" in execution["rules_matched"]
    assert execution["approved_by_human"] is True
    assert execution["final_status"] == "RECOVERED"
    assert run["gateway_mutation_attempts"] == 1


def test_scenario_d_gateway_timeout_unknown_then_resolved(scenario_runs):
    (run, _) = scenario_runs["D"]
    assert run["status_after_timeout"] == "UNKNOWN"
    assert run["status_after_requery"] == "UNKNOWN"  # paused, no blind retry
    assert run["final_status"] == "RECOVERED"  # resolved on gateway evidence
    # Exactly one mutating call across timeout, re-query, and resolution.
    assert run["gateway_mutation_attempts"] == 1


def test_scenario_e_unsafe_ai_recommendation_blocked(scenario_runs):
    (run, _) = scenario_runs["E"]
    assert run["forced_action_type"] == "refund"
    assert run["policy_outcome"] == "BLOCKED"
    assert "allowlist" in run["rules_matched"]
    assert "never_auto_execute.refund" in run["rules_matched"]
    assert run["final_status"] == "REJECTED"
    # The point of the scenario: not a single gateway call happened.
    assert run["gateway_mutation_attempts"] == 0
    assert run["audit_rows_for_action"] >= 3  # proposed, evaluated, rejected


# ---------------------------------------------------------------------------
# determinism: two fresh runs -> identical key numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_scenario_determinism(scenario_runs, name):
    run1, run2 = scenario_runs[name]
    assert _projection(run1) == _projection(run2)

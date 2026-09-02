"""DEF-03: the evaluation outcome path is measured, not prior.

The causal comparison used to be decided by a hand-set (failure-class x
action) CONVERSION table plus a flat GATEWAY_SUCCESS_RATE. These tests prove
the replacement: both arms are scored by rates MEASURED from the simulator's
own customer-behavior mechanism (re-attempt success, late-capture
self-resolution, organic return), fit on each arm's own data.

Properties under test:
1. measure_outcomes fits the simulator's observed behavior, deterministically.
2. Changing a simulator BEHAVIOR parameter (engine constants) changes the
   measured rates — the model tracks the simulator, not a hardcoded table.
3. No hand-set conversion priors remain in the runner's outcome path, and a
   behavior-parameter change moves a full run's measured recovery.
4. Per-class cells below MIN_CELL fall back to the pooled rate.
"""

import dataclasses
import shutil
import tempfile
from contextlib import contextmanager

import pytest
import sqlalchemy as sa

from app.models import Experiment
from app.services.evaluation import EvaluationRunner, measure_outcomes, outcomes
from app.services.evaluation import runner as runner_module
from app.services.revenue.classify import FailureClass
from app.simulator.cli import make_session
from app.simulator.config import SCENARIOS
from app.simulator.engine import run_simulation

_TINY = {"days": 2, "events": 1200, "customers": 60}
_RUN = dict(name="measured-it", scenario="upi_outage_demo", seed=11, **_TINY)


def _config(seed: int = 11):
    factory = SCENARIOS["upi_outage_demo"][1]
    return dataclasses.replace(
        factory(), seed=seed, days=_TINY["days"],
        target_events=_TINY["events"], customers=_TINY["customers"],
    )


@contextmanager
def _simulated_db(config):
    """A throwaway SQLite DB seeded by the real simulator (mirrors the
    runner's scratch-DB pattern)."""
    tmp = tempfile.mkdtemp(prefix="outcomes_test_")
    session = make_session(f"sqlite:///{tmp}/sim.db")
    try:
        run_simulation(config, session)
        yield session
    finally:
        bind = session.get_bind()
        session.close()
        bind.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. the measurement fits the simulator's own behavior, deterministically
# ---------------------------------------------------------------------------


def test_measured_rates_track_simulator_behavior():
    with _simulated_db(_config()) as db:
        model = measure_outcomes(db)
        again = measure_outcomes(db)

    # Deterministic: a pure function of the data (no RNG anywhere).
    assert model.to_dict() == again.to_dict()
    assert model.provenance == "measured_from_simulator_behavior"

    # The simulator retries ~15% of checkout failures at ~65% x reliability
    # and duns failed subscription cycles — re-attempts exist and mostly
    # succeed. Loose bands: tiny-scale cells are small, but the measured
    # rate must sit in the engine's behavioral regime, not a prior's.
    assert model.cells["reattempts_by_class"]
    assert 0.3 <= model.pooled_retry_success <= 1.0
    # Payment-level self-resolution is the late-capture quirk only: rare.
    assert 0.0 <= model.self_resolution <= 0.05
    assert model.cells["payments_with_failed_event"] > 0
    # Organic return is strictly smaller than re-attempt-conditional success
    # (it multiplies in the retry-propensity).
    assert 0.0 <= model.pooled_organic_return < model.pooled_retry_success
    # Every residual assumption is recorded with its value.
    assert model.assumptions and all(isinstance(a, str) for a in model.assumptions)


def test_column_mapping_is_the_only_draw_entry_point():
    with _simulated_db(_config()) as db:
        model = measure_outcomes(db)
    cls = FailureClass.SOFT_DECLINE
    assert model.rate_for("immediate_retry", cls) == model.retry_success[cls.value]
    # Delay-invariance (documented assumption): both retry columns share the
    # measured re-attempt rate.
    assert model.rate_for("delayed_retry", cls) == model.retry_success[cls.value]
    assert model.rate_for("payment_link", cls) == model.pooled_retry_success
    assert model.rate_for("notify", cls) == model.organic_return[cls.value]
    assert model.rate_for("no_action", cls) == model.self_resolution
    with pytest.raises(ValueError):
        model.rate_for("teleport", cls)


# ---------------------------------------------------------------------------
# 2. behavior parameters move the measurement (simulator-anchored, not fixed)
# ---------------------------------------------------------------------------


def test_reattempt_success_parameter_moves_measured_rate(monkeypatch):
    with _simulated_db(_config()) as db:
        baseline_model = measure_outcomes(db)

    # Same seed, same config — only the simulator's behavior parameters move.
    monkeypatch.setattr("app.simulator.engine.CHECKOUT_RETRY_SUCCESS", 0.10)
    monkeypatch.setattr("app.simulator.engine.SUB_RETRY_SUCCESS", (0.08, 0.06, 0.04))
    with _simulated_db(_config()) as db:
        weakened = measure_outcomes(db)

    assert baseline_model.pooled_retry_success >= 0.5
    assert weakened.pooled_retry_success <= 0.25
    assert weakened.pooled_retry_success < baseline_model.pooled_retry_success


def test_late_capture_parameter_moves_measured_self_resolution(monkeypatch):
    monkeypatch.setattr("app.simulator.engine.LATE_CAPTURE_RATE", 0.0)
    with _simulated_db(_config()) as db:
        suppressed = measure_outcomes(db)
    assert suppressed.self_resolution == 0.0
    assert suppressed.self_resolution_lags_minutes == ()

    monkeypatch.setattr("app.simulator.engine.LATE_CAPTURE_RATE", 0.50)
    with _simulated_db(_config()) as db:
        amplified = measure_outcomes(db)
    assert amplified.self_resolution >= 0.25
    assert amplified.self_resolution_lags_minutes  # empirical lags exist
    # The lag bootstrap distribution is the simulator's own (30s-15min).
    assert max(amplified.self_resolution_lags_minutes) <= 16.0


# ---------------------------------------------------------------------------
# 3. the runner's outcome path carries no priors and uses the measured model
# ---------------------------------------------------------------------------


def test_no_handset_priors_remain_in_the_outcome_path():
    for name in ("CONVERSION", "GATEWAY_SUCCESS_RATE", "SELF_RESOLUTION_MAX_LAG_MINUTES"):
        assert not hasattr(runner_module, name), name


def test_run_records_measured_model_on_both_arms_and_experiment(db_session):
    run = EvaluationRunner(db_session).run(**_RUN, holdout_fraction=0.25)
    assert run.status == "completed"
    m = run.metrics

    model = m["outcome_model"]
    assert model["provenance"] == "measured_from_simulator_behavior"
    assert model["assumptions"]
    # Both arms ran the same config -> same data -> the SAME fitted generator.
    assert m["arms"]["baseline"]["outcome_model"] == m["arms"]["pulsecover"]["outcome_model"]

    # The experiment's pre-registered config names the definitions; the
    # fitted rates land on the completed row. No prior table survives.
    experiment = db_session.scalar(
        sa.select(Experiment).where(Experiment.name == f"measured-it:{run.id}")
    )
    assert experiment is not None
    assert "conversion_model" not in experiment.config
    assert "gateway_success_rate" not in experiment.config
    assert "baseline_definition" in experiment.config
    assert "treatment_definition" in experiment.config
    assert experiment.config["outcome_model_measured"] == model

    # The baseline arm's realized recovery tracks the measured re-attempt
    # rate (per-payment seeded draws, not a table lookup).
    baseline = m["arms"]["baseline"]
    if baseline["failed_amount_paise"]:
        assert abs(baseline["recovery_rate"] - model["pooled_retry_success"]) < 0.15


def test_full_run_recovery_moves_with_simulator_behavior(db_session, monkeypatch):
    """The decisive measured-not-prior proof at run level: same seed, same
    scenario — only the simulator's re-attempt behavior weakens, and the
    baseline arm's measured recovery collapses with it."""
    first = EvaluationRunner(db_session).run(**_RUN, holdout_fraction=0.25)
    assert first.status == "completed"
    rate_default = first.metrics["arms"]["baseline"]["recovery_rate"]

    monkeypatch.setattr("app.simulator.engine.CHECKOUT_RETRY_SUCCESS", 0.10)
    monkeypatch.setattr("app.simulator.engine.SUB_RETRY_SUCCESS", (0.08, 0.06, 0.04))
    second = EvaluationRunner(db_session).run(
        **{**_RUN, "name": "measured-it-weakened"}, holdout_fraction=0.25
    )
    assert second.status == "completed"
    weakened = second.metrics["arms"]["baseline"]
    assert weakened["outcome_model"]["pooled_retry_success"] <= 0.25
    assert weakened["recovery_rate"] < rate_default * 0.5


# ---------------------------------------------------------------------------
# 4. small cells pool; missing classes pool
# ---------------------------------------------------------------------------


def test_per_class_rates_fall_back_to_pooled_below_min_cell():
    hits = {"a": 1, "b": 50}
    totals = {"a": 2, "b": 100}  # a: 2 < MIN_CELL -> pooled; b: 0.5 measured
    rates, pooled = outcomes._per_class(hits, totals)
    assert pooled == 51 / 102
    assert rates["a"] == pooled
    assert rates["b"] == 0.5

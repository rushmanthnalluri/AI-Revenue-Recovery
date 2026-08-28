"""Cross-day reproducibility: the pinned ``end_date`` must reach the
simulator config, the stored run record must carry every version/anchor the
metrics depend on, and two runs pinned to the same anchor must produce
bit-identical metrics (wall-clock MTTR excepted) — regardless of the
calendar day the suite runs on. Runs at the tiny plumbing scale."""

import dataclasses
import json
from datetime import datetime, timezone

import sqlalchemy as sa

from app.models import Experiment
from app.services.diagnosis.heuristic import HEURISTIC_VERSION
from app.services.diagnosis.service import DEFAULT_ARTIFACTS_DIR
from app.services.diagnosis.training import ACTIVE_POINTER
from app.services.evaluation import EvaluationRunner, dataset_version, resolve_anchor
from app.services.policy.config import load_policy_config
from app.simulator.config import SCENARIOS

_TINY = {"days": 2, "events": 1200, "customers": 60}
_RUN = dict(name="repro-it", scenario="upi_outage_demo", seed=11, **_TINY)

# A pin that is deliberately NOT "today" on any machine: the assertions below
# can only pass if the pin — not the calendar — anchored the dataset.
PIN_A = datetime(2026, 8, 20, tzinfo=timezone.utc)
PIN_B = datetime(2026, 8, 21, tzinfo=timezone.utc)


def _expected_config(end_date: datetime) -> object:
    factory = SCENARIOS["upi_outage_demo"][1]
    return dataclasses.replace(
        factory(), seed=11, days=2, target_events=1200, customers=60, end_date=end_date
    )


def _strip_wallclock(metrics: dict) -> dict:
    """Wall-clock pipeline latency (MTTR) is an operational measurement and
    legitimately varies run to run; everything simulator-derived must be
    bit-identical between same-anchor runs."""
    m = dict(metrics)
    m.pop("mean_time_to_recover_minutes", None)
    arms = dict(m["arms"])
    pulsecover = dict(arms["pulsecover"])
    pulsecover.pop("mttr_minutes", None)
    arms["pulsecover"] = pulsecover
    m["arms"] = arms
    return m


def test_end_date_reaches_the_simulator_config(db_session):
    """The pin must flow into the config both arms run with: the recorded
    simulator_run_id is the hash of the pinned config (a different anchor is
    a different dataset with a different id), and the stored anchor is the
    pin itself."""
    run = EvaluationRunner(db_session).run(**_RUN, end_date=PIN_A, holdout_fraction=0.25)
    assert run.status == "completed"

    expected = _expected_config(PIN_A)
    assert run.metrics["dataset"]["simulator_run_id"] == expected.run_id
    assert run.metrics["arms"]["baseline"]["simulator_run_id"] == expected.run_id
    assert run.metrics["arms"]["pulsecover"]["simulator_run_id"] == expected.run_id
    assert run.metrics["dataset"]["anchor"] == "2026-08-20T00:00:00+00:00"
    assert run.metrics["dataset"]["end_date"] == "2026-08-20T00:00:00+00:00"
    assert run.metrics["dataset"]["dataset_version"] == f"{expected.run_id}@2026-08-20"

    # A different pin is a different dataset: anchor, version and run id all
    # move. (Unset would have anchored to today 00:00 UTC instead — see
    # docs/evaluation.md §1.)
    other = EvaluationRunner(db_session).run(**_RUN, end_date=PIN_B, holdout_fraction=0.25)
    assert other.status == "completed"
    assert other.metrics["dataset"]["anchor"] == "2026-08-21T00:00:00+00:00"
    assert other.metrics["dataset"]["dataset_version"].endswith("@2026-08-21")
    assert (
        other.metrics["dataset"]["simulator_run_id"]
        != run.metrics["dataset"]["simulator_run_id"]
    )


def test_run_record_carries_all_version_and_anchor_fields(db_session):
    """A stored run must be reproducible from its own row: scenario, seed,
    simulator run id, pinned end_date, resolved anchor, anchor-qualified
    dataset version, the diagnosis artifact id, and the policy version —
    in metrics AND in the pre-registered experiment config."""
    run = EvaluationRunner(db_session).run(**_RUN, end_date=PIN_A, holdout_fraction=0.25)
    assert run.status == "completed"

    dataset = run.metrics["dataset"]
    assert dataset["scenario"] == "upi_outage_demo"
    assert dataset["seed"] == 11
    assert dataset["simulator_run_id"] == _expected_config(PIN_A).run_id
    assert dataset["end_date"] == "2026-08-20T00:00:00+00:00"
    assert dataset["anchor"] == "2026-08-20T00:00:00+00:00"
    assert dataset["dataset_version"] == dataset_version(
        _expected_config(PIN_A), resolve_anchor(_expected_config(PIN_A))
    )

    # Versions must name what the arms actually used: the active diagnosis
    # pointer (or the heuristic fallback) and the content-hashed policy file.
    try:
        pointer = json.loads(
            (DEFAULT_ARTIFACTS_DIR / ACTIVE_POINTER).read_text(encoding="utf-8")
        )
        expected_artifact = f"{pointer['algo']} {pointer['model_version']}"
    except OSError:
        expected_artifact = HEURISTIC_VERSION
    assert run.metrics["versions"]["diagnosis_artifact"] == expected_artifact
    assert run.metrics["versions"]["policy"] == load_policy_config().policy_version

    experiment = db_session.scalar(
        sa.select(Experiment).where(Experiment.name == f"repro-it:{run.id}")
    )
    assert experiment is not None
    assert experiment.config["end_date"] == "2026-08-20T00:00:00+00:00"
    assert experiment.config["dataset"] == dataset
    assert experiment.config["versions"] == run.metrics["versions"]
    # The pinned end_date is inside the hashed base config too, so the
    # simulator run id on the row is reproducible from the experiment alone.
    assert experiment.config["base_config"]["end_date"] == "2026-08-20T00:00:00+00:00"


def test_pinned_anchor_is_deterministic_across_runs(db_session):
    """Two runs with the same pinned end_date reproduce identical metrics
    (wall-clock MTTR excepted) — the property that makes the canonical spec
    (ml/experiments/canonical_spec.json) reproducible on any day."""
    first = EvaluationRunner(db_session).run(**_RUN, end_date=PIN_A, holdout_fraction=0.25)
    second = EvaluationRunner(db_session).run(**_RUN, end_date=PIN_A, holdout_fraction=0.25)
    assert first.status == second.status == "completed"
    assert (
        first.metrics["dataset"]["dataset_version"]
        == second.metrics["dataset"]["dataset_version"]
    )
    assert json.dumps(_strip_wallclock(first.metrics), sort_keys=True) == json.dumps(
        _strip_wallclock(second.metrics), sort_keys=True
    )

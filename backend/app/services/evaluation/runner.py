"""Evaluation harness — scientific scoring of PulseRecover vs a naive baseline.

Methodology (full write-up in docs/evaluation.md):

- Each arm runs against its OWN scratch SQLite database, seeded by the real
  simulator with the same deterministic config — the main/demo database is
  never touched, and ground truth comes from ``simulator_ground_truth``.
- BASELINE: a generic single retry (fresh order via the gateway twin) for
  EVERY failed payment. No detection, no diagnosis, no policy gate, no
  verification — that is the point of the comparison.
- PULSECOVER: the real loop, unchanged — scheduled detection passes
  (``run_detection``), ML diagnosis (``DiagnosisService``), opportunity +
  strategy generation, the deterministic policy gate (``RecoveryExecutor``),
  and verification through the real webhook handler registry
  (``app.api.v1.webhooks.EVENT_HANDLERS``) / ``RecoveryExecutor.resolve``.
- The harness plays two clearly-simulated roles, both deterministic:
  * the human operator — approves every action the gate sends to
    PENDING_APPROVAL (actor ``human:eval_operator``);
  * the customer — answers retries/links/notifications via a documented
    (failure-class x action) conversion table. Draws are seeded on stable
    simulator identities (payment/order id), and app-id allocation inside
    each arm runs under a deterministic-id guard, so two runs with the same
    seed reproduce identical metrics (wall-clock MTTR excepted — it is an
    operational measurement, not a simulator output); payment links are
    decided by the gateway twin itself.
- HOLDOUT (pre-registered, docs/product-strategy.md §4.1): within the
  PulseCover arm a deterministic customer-level holdout
  (``app.services.evaluation.holdout``) receives NO recovery actions —
  opportunities are never built for held-out customers; detection/diagnosis
  still run fleet-wide. Both groups share the organic baseline: failures the
  loop never executed an action against self-resolve with the ``no_action``
  column of the same conversion prior, captured through the real webhook
  path. The run reports incremental lift = rate(treatment) − rate(holdout)
  with a Newcombe/Wilson 95% CI (metrics["holdout"]).
- Safety invariant: a PulseCover-arm action may reach EXECUTING or beyond ONLY
  with an ALLOWED policy decision or a recorded human approval. The harness
  counts violations and the test suite asserts the count is 0.

MTTD is measured in simulator time (first detection pass whose window
overlaps the injected window minus the injected start). MTTR is wall-clock
pipeline latency (proposed_at -> verified_at): sim-time MTTR is meaningless
when execution is synchronous, so the honest operational number is reported
and labeled as such.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import math
import random
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import ids as ids_module
from app.api.v1.webhooks import EVENT_HANDLERS  # the real verification path
from app.db import utcnow
from app.logging import get_logger
from app.models import (
    EvaluationRun,
    Experiment,
    Incident,
    ModelPrediction,
    Payment,
    PaymentEvent,
    PolicyDecisionRecord,
    RecoveryAction,
    RecoveryOpportunity,
    RecoveryStrategy,
    SimulatorGroundTruth,
    WebhookEvent,
)
from app.ports import ActionType, PolicyOutcome, RecoveryStatus
from app.schemas.detection import DetectionRunRequest
from app.services.detection import run_detection
from app.services.detection.series import latest_event_anchor
from app.services.diagnosis.heuristic import HEURISTIC_VERSION
from app.services.diagnosis.service import DEFAULT_ARTIFACTS_DIR, DiagnosisError, DiagnosisService
from app.services.diagnosis.taxonomy import CauseLabel
from app.services.diagnosis.training import ACTIVE_POINTER
from app.services.evaluation.holdout import (
    CI_Z,
    DEFAULT_HOLDOUT_FRACTION,
    HoldoutExcludingBuilder,
    is_holdout,
    median,
    newcombe_ci,
)
from app.services.policy.config import PolicyConfigError, load_policy_config
from app.services.razorpay.simulated import SimulatedPaymentGateway
from app.services.recovery import OpportunityBuilder, RecoveryExecutor, StrategyGenerator
from app.services.revenue.classify import FailureClass, classify_failure
from app.simulator.cli import make_session
from app.simulator.config import SCENARIOS, SimulatorConfig
from app.simulator.engine import run_simulation

logger = get_logger("app.services.evaluation")

# Gateway twin success rate for its inline decisions (payment links). Links
# are decided INSIDE the twin (a flat, class-independent rate — a documented
# twin limitation); all other customer outcomes use the CONVERSION table.
GATEWAY_SUCCESS_RATE = 0.35

# Customer conversion model: P(the customer pays | failure class x the action
# taken). ONE table governs both arms — the baseline always lands in the
# immediate_retry column; PulseCover lands wherever its strategy chose.
# Priors follow docs/revenue-methodology.md: a single immediate retry rarely
# fixes insufficient funds (the money is still not there) but often fixes a
# transient timeout; a payday-aware delayed retry and a fresh payment link do
# better on funds/abandonment; never-approve (hard) declines stay near zero.
#
# The no_action column is the holdout's counterfactual prior: P(the payment
# self-resolves within the attribution window with ZERO intervention) — the
# organic recovery that gross attribution wrongly credits to tools (see
# docs/competitive-analysis.md §5: Recurly observes material self-resolution
# on soft declines). Every action column sits above no_action for the same
# class except hard declines, where both stay near zero. Like the rest of
# the table it is a documented prior, not a measurement; one table governs
# treatment and holdout alike, so the comparison stays fair.
CONVERSION: dict[FailureClass, dict[str, float]] = {
    FailureClass.TIMEOUT: {
        "immediate_retry": 0.55,
        "delayed_retry": 0.50,
        "payment_link": 0.50,
        "notify": 0.30,
        "no_action": 0.30,
    },
    FailureClass.SOFT_DECLINE: {
        "immediate_retry": 0.30,
        "delayed_retry": 0.35,
        "payment_link": 0.35,
        "notify": 0.20,
        "no_action": 0.20,
    },
    FailureClass.INSUFFICIENT_FUNDS: {
        "immediate_retry": 0.08,
        "delayed_retry": 0.35,
        "payment_link": 0.20,
        "notify": 0.15,
        "no_action": 0.08,
    },
    FailureClass.ABANDONMENT: {
        "immediate_retry": 0.15,
        "delayed_retry": 0.12,
        "payment_link": 0.30,
        "notify": 0.20,
        "no_action": 0.06,
    },
    FailureClass.HARD_DECLINE: {
        "immediate_retry": 0.02,
        "delayed_retry": 0.02,
        "payment_link": 0.03,
        "notify": 0.02,
        "no_action": 0.01,
    },
    FailureClass.UNKNOWN: {
        "immediate_retry": 0.15,
        "delayed_retry": 0.15,
        "payment_link": 0.18,
        "notify": 0.10,
        "no_action": 0.06,
    },
}

# Holdout self-resolution lag model: a self-resolving payment pays within a
# uniform (0, 7 days] lag; resolutions sampled past the scenario-end anchor
# are right-censored and counted as NOT recovered within the window.
SELF_RESOLUTION_MAX_LAG_MINUTES = 7 * 24 * 60

# Detection schedule: passes half-tile the simulated window (pass every STEP,
# each looking back WINDOW). 12h windows give the zscore detector enough
# pre-anomaly baseline to fire on 1.5-3h injected incidents (measured:
# docs/evaluation.md); detector/thresholds stay at production defaults.
DETECTION_STEP_MINUTES = 360
DETECTION_WINDOW_MINUTES = 720

OPERATOR = "human:eval_operator"

#: Simulator IncidentKind -> diagnosis CauseLabel. method_outage with a
#: targeted bank is a bank downtime; without one, a method outage.
KIND_TO_CAUSE: dict[str, str] = {
    "gateway_degradation": CauseLabel.GATEWAY_DEGRADATION.value,
    "route_latency": CauseLabel.ROUTE_LATENCY.value,
    "method_outage": CauseLabel.METHOD_OUTAGE.value,
    "checkout_abandonment_spike": CauseLabel.ABANDONMENT_SPIKE.value,
    "subscription_failure_spike": CauseLabel.SUBSCRIPTION_FAILURE_SPIKE.value,
    "customer_insufficient_funds_wave": CauseLabel.CUSTOMER_INSUFFICIENT_FUNDS_WAVE.value,
}


def truth_cause(truth: dict[str, Any]) -> str:
    kind = str(truth.get("kind", ""))
    if kind == "method_outage" and (truth.get("params") or {}).get("bank"):
        return CauseLabel.BANK_DOWNTIME.value
    return KIND_TO_CAUSE.get(kind, CauseLabel.NO_FAULT.value)


def resolve_anchor(config: SimulatorConfig) -> datetime:
    """The window right edge the simulator engine will use for this config —
    the pinned ``end_date`` or *today* 00:00 UTC when unset (mirrors
    ``app/simulator/engine.py``). Recorded on every run so the dataset anchor
    is never implicit."""
    anchor = config.end_date
    if anchor is None:
        anchor = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    elif anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return anchor


def dataset_version(config: SimulatorConfig, anchor: datetime) -> str:
    """Anchor-qualified dataset version: the deterministic simulator run id
    (seed + config hash) plus the resolved anchor date. Same seed on a
    different anchor is a different dataset; this string separates them."""
    return f"{config.run_id}@{anchor.date().isoformat()}"


def _active_diagnosis_artifact_id(artifacts_dir: Path | str | None) -> str:
    """``<algo> <model_version>`` of the artifact the DiagnosisService will
    load (the active pointer), or the heuristic fallback version when no
    pointer exists — exactly what the arm's predictions will carry."""
    directory = Path(artifacts_dir) if artifacts_dir else DEFAULT_ARTIFACTS_DIR
    try:
        pointer = json.loads((directory / ACTIVE_POINTER).read_text(encoding="utf-8"))
        return f"{pointer['algo']} {pointer['model_version']}"
    except (OSError, ValueError, KeyError):
        return HEURISTIC_VERSION


def _policy_version() -> str:
    """The policy file version the gate will evaluate with (content-hashed by
    the loader). A broken file fails the run downstream in the executor; the
    record notes it rather than crashing the bookkeeping."""
    try:
        return load_policy_config().policy_version
    except PolicyConfigError as exc:
        return f"error: {exc}"


def _parse_ts(value: Any) -> datetime:
    ts = datetime.fromisoformat(str(value))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _stable_subject(
    opp: RecoveryOpportunity | None,
    payment: Payment | None,
    action: RecoveryAction,
) -> str:
    """Stable identity for seeding the customer-conversion draw: the
    simulator payment id (or order id for dropped checkouts) — never a
    per-run random id — so outcomes reproduce across identical-seed runs
    even outside the deterministic-id guard."""
    if payment is not None:
        return payment.id
    order_id = ((opp.meta or {}) if opp else {}).get("order_id")
    return str(order_id or action.gateway_request_id)


@dataclass
class GroundTruthIncident:
    entity_id: str
    kind: str
    cause: str
    start: datetime
    end: datetime
    recoverable: bool
    affected_amount_paise: int


@dataclass
class ArmResult:
    arm: str
    simulator_run_id: str
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScopedFailure:
    """One first-attempt failed payment in the lift estimand's scope — the
    pre-registered denominator is ALL of these, snapshotted BEFORE any
    recovery action (a post-hoc query would drop verified recoveries, whose
    payments flip to captured: cherry-picking by construction)."""

    payment_id: str
    gateway_payment_id: str | None
    customer_id: str | None
    amount_paise: int
    method: str | None
    failure_class: FailureClass
    failed_at: datetime
    is_holdout: bool


def load_ground_truth(session: Session, run_id: str) -> list[GroundTruthIncident]:
    rows = session.scalars(
        sa.select(SimulatorGroundTruth).where(
            SimulatorGroundTruth.simulator_run_id == run_id,
            SimulatorGroundTruth.entity_type == "incident",
        )
    )
    out = []
    for row in rows:
        truth = dict(row.truth or {})
        out.append(
            GroundTruthIncident(
                entity_id=row.entity_id,
                kind=str(truth.get("kind", "")),
                cause=truth_cause(truth),
                start=_parse_ts(truth["start"]),
                end=_parse_ts(truth["end"]),
                recoverable=bool(truth.get("recoverable")),
                affected_amount_paise=int(truth.get("affected_amount_paise") or 0),
            )
        )
    out.sort(key=lambda g: (g.start, g.entity_id))
    return out


def _overlaps(incident: Incident, gt: GroundTruthIncident) -> bool:
    """A detected incident matches ground truth when its flagged span (or,
    lacking one, its analysis window) overlaps the injected window."""
    meta = incident.meta or {}
    try:
        start = _parse_ts(meta["anomaly_start"])
        end = _parse_ts(meta["anomaly_end"])
    except (KeyError, ValueError):
        start = incident.window_start or incident.detected_at
        end = incident.window_end or incident.detected_at
    return start < gt.end and gt.start < end


class _ScratchDb:
    """A throwaway SQLite database (tempfile) with simulator speed pragmas."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="pulserecover_eval_"))
        self.url = f"sqlite:///{self.dir / 'eval.db'}"
        self.session: Session = make_session(self.url)

    def close(self) -> None:
        bind = self.session.get_bind()
        self.session.close()
        bind.dispose()
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self) -> "_ScratchDb":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EvaluationRunner:
    """Runs one baseline-vs-PulseRecover experiment and persists the result."""

    def __init__(self, session: Session, *, artifacts_dir: Path | str | None = None) -> None:
        self._main = session
        self._artifacts_dir = artifacts_dir

    # ------------------------------------------------------------------
    # reproducibility
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _deterministic_ids(self):
        """Reproducibility guard for one arm. ``app.ids.new_id`` normally
        returns uuid4-random ids, and the recovery pipeline threads those ids
        into gateway request ids — every draw keyed on a request id (the
        harness's conversion table, the twin's payment-link outcome) would
        otherwise differ across identical-seed runs. Inside this guard, ids
        are prefix + a deterministic counter with a non-hex marker, so the
        same seed replays the same id sequence (and the marker can never
        collide with a real uuid4hex). Scoped to scratch-DB phases only;
        main-DB rows are created outside it. Wall-clock metrics (MTTR) are
        operational measurements and legitimately vary run to run."""
        original = ids_module.new_id
        counter = itertools.count()
        ids_module.new_id = lambda prefix: f"{prefix}d3t{next(counter):029x}"
        try:
            yield
        finally:
            ids_module.new_id = original

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        name: str,
        scenario: str,
        seed: int | None = None,
        days: int | None = None,
        events: int | None = None,
        customers: int | None = None,
        evaluation_type: str = "end_to_end",
        holdout_fraction: float | None = None,
        end_date: datetime | None = None,
    ) -> EvaluationRun:
        if scenario not in SCENARIOS:
            raise ValueError(
                f"unknown scenario {scenario!r}; known: {', '.join(sorted(SCENARIOS))}"
            )
        fraction = DEFAULT_HOLDOUT_FRACTION if holdout_fraction is None else holdout_fraction
        if not 0.0 <= fraction < 1.0:
            raise ValueError(
                f"holdout_fraction must be in [0, 1); got {holdout_fraction!r} "
                "(0 disables the holdout)"
            )
        factory = SCENARIOS[scenario][1]
        base: SimulatorConfig = factory()  # type: ignore[operator]
        overrides = {
            k: v
            for k, v in {
                "seed": seed,
                "days": days,
                "target_events": events,
                "customers": customers,
                "end_date": end_date,
            }.items()
            if v is not None
        }
        config = dataclasses.replace(base, **overrides)

        # Run-record completeness: pin down every version/anchor the metrics
        # depend on, so a stored run is fully reproducible from its own row.
        anchor = resolve_anchor(config)
        dataset = {
            "scenario": scenario,
            "seed": config.seed,
            "simulator_run_id": config.run_id,
            "end_date": config.end_date.isoformat() if config.end_date else None,
            "anchor": anchor.isoformat(),
            "dataset_version": dataset_version(config, anchor),
        }
        versions = {
            "diagnosis_artifact": _active_diagnosis_artifact_id(self._artifacts_dir),
            "policy": _policy_version(),
        }

        run = EvaluationRun(
            name=name,
            evaluation_type=evaluation_type,
            dataset=scenario,
            simulator_run_id=None,  # arms run in scratch DBs; ids live in metrics
            status="running",
            started_at=utcnow(),
            metrics={},
        )
        self._main.add(run)
        self._main.flush()
        experiment = Experiment(
            name=f"{name}:{run.id}",
            description=f"baseline-vs-pulserecover evaluation over scenario {scenario!r}",
            hypothesis=(
                "Policy-gated, diagnosis-driven recovery recovers more revenue with "
                "orders-of-magnitude fewer interventions than retry-everything."
            ),
            config={
                "scenario": scenario,
                "seed": seed,
                "days": days,
                "events": events,
                "customers": customers,
                "end_date": end_date.isoformat() if end_date else None,
                "base_config": config.config_dict(),
                "dataset": dataset,
                "versions": versions,
                "detection": {
                    "step_minutes": DETECTION_STEP_MINUTES,
                    "window_minutes": DETECTION_WINDOW_MINUTES,
                    "detector": "production defaults",
                },
                "gateway_success_rate": GATEWAY_SUCCESS_RATE,
                "conversion_model": {
                    k.value: v for k, v in CONVERSION.items()
                },
                "holdout": {
                    "fraction": fraction,
                    "assignment": "customer-level; sha256('holdout:{seed}:{customer_id}') "
                    "first 8 bytes / 2^64 < fraction",
                    "estimand": "incremental lift = recovery_rate(treatment) - "
                    "recovery_rate(holdout) over the run's fixed attribution "
                    "window; denominators are ALL first-attempt failed "
                    "payments per group; recovery = verified captures only; "
                    "both groups share the organic no_action baseline",
                    "ci_method": "newcombe_hybrid_score_wilson_95",
                    "self_resolution_lag": "uniform(0, 7 days], right-censored "
                    "at the scenario-end anchor",
                },
            },
            status="running",
            started_at=run.started_at,
        )
        self._main.add(experiment)
        self._main.commit()

        t0 = time.perf_counter()
        try:
            baseline = self._run_baseline(config)
            pulsecover = self._run_pulsecover(config, holdout_fraction=fraction)
            metrics = self._assemble(baseline, pulsecover)
            # Additive run-record completeness (older runs lack these keys):
            # the dataset anchor/version and the model/policy versions the
            # metrics depend on.
            metrics["dataset"] = dataset
            metrics["versions"] = versions
            run.status = "completed"
            run.metrics = metrics
            run.finished_at = utcnow()
            run.notes = (
                "Arms ran in isolated scratch DBs; simulator_run_ids are in "
                "metrics.arms.*.simulator_run_id. MTTD is simulator-time; "
                "MTTR is wall-clock pipeline latency. The PulseCover arm "
                "withholds all recovery actions from a deterministic "
                "customer-level holdout (metrics.holdout); incremental lift "
                "is reported with a Newcombe/Wilson 95% CI."
            )
            experiment.status = "completed"
            experiment.ended_at = run.finished_at
            experiment.results = {
                "comparison": metrics.get("comparison", {}),
                "holdout_lift": (metrics.get("holdout") or {}).get("lift"),
                "runtime_ms": int((time.perf_counter() - t0) * 1000),
            }
        except Exception as exc:  # persist the failure honestly, then surface it
            logger.exception("evaluation run failed")
            run.status = "failed"
            run.finished_at = utcnow()
            run.notes = f"{type(exc).__name__}: {exc}"
            experiment.status = "aborted"
            experiment.ended_at = run.finished_at
            self._main.commit()
            raise
        self._main.commit()
        return run

    # ------------------------------------------------------------------
    # metric assembly
    # ------------------------------------------------------------------

    def _assemble(self, baseline: ArmResult, pulsecover: ArmResult) -> dict[str, Any]:
        b, p = baseline.metrics, pulsecover.metrics
        b_rec, p_rec = b["recovered_revenue_paise"], p["recovered_revenue_paise"]
        comparison = {
            "recovered_revenue_delta_paise": p_rec - b_rec,
            "recovered_revenue_ratio": (round(p_rec / b_rec, 4) if b_rec else None),
            "recovery_rate_delta": round(
                p["recovery_rate"] - b["recovery_rate"], 6
            ),
            "interventions_baseline": b["interventions_count"],
            "interventions_pulserecover": p["interventions_count"],
            "intervention_reduction": (
                round(1 - p["interventions_count"] / b["interventions_count"], 4)
                if b["interventions_count"]
                else None
            ),
            "false_interventions_baseline": b["false_interventions_count"],
            "false_interventions_pulserecover": p["false_interventions_count"],
        }
        assembled = {
            "arms": {"baseline": b, "pulsecover": p},
            "comparison": comparison,
            # Top-level keys feed GET /api/v1/evaluation/metrics aggregation.
            "detection_precision": p["detection"]["precision"],
            "detection_recall": p["detection"]["recall"],
            "detection_f1": p["detection"]["f1"],
            "mean_time_to_detect_minutes": p["detection"]["mttd_minutes"],
            "diagnosis_top1_accuracy": p["diagnosis"]["top1_accuracy"],
            "diagnosis_top3_accuracy": p["diagnosis"]["top3_accuracy"],
            "recovery_rate": p["recovery_rate"],
            "recovered_revenue_paise": p_rec,
            "mean_time_to_recover_minutes": p["mttr_minutes"],
            "false_action_rate": p["false_action_rate"],
            "unsafe_action_count": p["unsafe_action_count"],
            "baseline_recovery_rate": b["recovery_rate"],
            "baseline_recovered_revenue_paise": b_rec,
        }
        holdout = p.get("holdout")
        if isinstance(holdout, dict):
            # Additive: the randomized-holdout section and its headline
            # aggregates (older runs simply lack these keys).
            assembled["holdout"] = holdout
            lift = holdout.get("lift") or {}
            assembled["incremental_lift"] = lift.get("point")
            assembled["incremental_lift_ci95_low"] = lift.get("ci95_low")
            assembled["incremental_lift_ci95_high"] = lift.get("ci95_high")
            assembled["treatment_recovery_rate"] = (holdout.get("treatment") or {}).get(
                "recovery_rate"
            )
            assembled["holdout_recovery_rate"] = (holdout.get("holdout") or {}).get(
                "recovery_rate"
            )
        return assembled

    # ------------------------------------------------------------------
    # BASELINE arm: one ungated retry per failed payment
    # ------------------------------------------------------------------

    def _run_baseline(self, config: SimulatorConfig) -> ArmResult:
        with _ScratchDb() as scratch:
            db = scratch.session
            sim = run_simulation(config, db)
            gateway = SimulatedPaymentGateway(
                seed=config.seed, success_rate=GATEWAY_SUCCESS_RATE
            )
            failed = list(
                db.scalars(sa.select(Payment).where(Payment.status == "failed"))
            )
            recovered = 0
            false_interventions = 0
            false_amount = 0
            for payment in failed:
                # The baseline's only "policy": fire one retry, always.
                gateway.create_order(
                    amount_paise=payment.amount_paise,
                    currency=payment.currency or "INR",
                    idempotency_key=f"baseline:{payment.id}",
                )
                cls = classify_failure(payment)
                if cls is FailureClass.HARD_DECLINE:
                    false_interventions += 1
                    false_amount += payment.amount_paise
                rng = random.Random(f"eval:{config.seed}:baseline:{payment.id}")
                if rng.random() < CONVERSION[cls]["immediate_retry"]:
                    recovered += payment.amount_paise
            failed_amount = sum(p.amount_paise for p in failed)
            metrics = {
                "simulator_run_id": sim.run_id,
                "failed_payments_count": len(failed),
                "failed_amount_paise": failed_amount,
                "interventions_count": len(failed),  # retries everything
                "ungated_actions_count": len(failed),  # no policy gate at all
                "false_interventions_count": false_interventions,
                "false_intervention_amount_paise": false_amount,
                "recovered_revenue_paise": recovered,
                "recovery_rate": round(recovered / failed_amount, 6) if failed_amount else 0.0,
            }
            return ArmResult(arm="baseline", simulator_run_id=sim.run_id, metrics=metrics)

    # ------------------------------------------------------------------
    # PULSECOVER arm: the real loop
    # ------------------------------------------------------------------

    def _run_pulsecover(
        self, config: SimulatorConfig, *, holdout_fraction: float
    ) -> ArmResult:
        with _ScratchDb() as scratch:
            db = scratch.session
            sim = run_simulation(config, db)
            gateway = SimulatedPaymentGateway(
                seed=config.seed, success_rate=GATEWAY_SUCCESS_RATE
            )
            gt = load_ground_truth(db, sim.run_id)

            # Snapshot the estimand's scope BEFORE any recovery action runs:
            # verification webhooks flip recovered payments to `captured`, so
            # a post-hoc "status = failed" query would silently drop exactly
            # the payments the loop recovered. The scenario-end anchor (the
            # attribution window's right edge) is captured now too, while the
            # event stream is still purely simulator-time.
            anchor = latest_event_anchor(db)
            scope = self._snapshot_scope(db, config, holdout_fraction)

            # Everything from detection onward allocates app ids; the guard
            # makes the id sequence (and therefore every draw keyed on it)
            # reproducible across identical-seed runs.
            with self._deterministic_ids():
                detection = self._detect(db, gt)
                diagnosis = self._diagnose(db, gt)
                recovery = self._recover(
                    db, config, gateway, holdout_fraction=holdout_fraction
                )
                holdout = (
                    self._evaluate_holdout(
                        db, config, gateway, scope, anchor, holdout_fraction
                    )
                    if holdout_fraction > 0.0
                    else None
                )

            failed_amount = sum(f.amount_paise for f in scope)
            metrics: dict[str, Any] = {
                "simulator_run_id": sim.run_id,
                "ground_truth_incidents": len(gt),
                "detection": detection,
                "diagnosis": diagnosis,
                **recovery,
                # Pre-recovery snapshot: ALL first-attempt failed payments,
                # including ones later verified recovered.
                "failed_payments_count": len(scope),
                "failed_amount_paise": failed_amount,
            }
            if holdout is not None:
                metrics["holdout"] = holdout
            rec = recovery["recovered_revenue_paise"]
            metrics["recovery_rate"] = (
                round(rec / failed_amount, 6) if failed_amount else 0.0
            )
            return ArmResult(arm="pulsecover", simulator_run_id=sim.run_id, metrics=metrics)

    def _snapshot_scope(
        self, db: Session, config: SimulatorConfig, holdout_fraction: float
    ) -> list[ScopedFailure]:
        """ALL first-attempt failed payments, partitioned into treatment vs
        holdout by their customer's deterministic assignment."""
        failed = list(
            db.scalars(
                sa.select(Payment)
                .where(Payment.status == "failed")
                .order_by(Payment.created_at, Payment.id)
            )
        )
        scope: list[ScopedFailure] = []
        for payment in failed:
            failed_at = payment.created_at
            if failed_at.tzinfo is None:
                failed_at = failed_at.replace(tzinfo=timezone.utc)
            scope.append(
                ScopedFailure(
                    payment_id=payment.id,
                    gateway_payment_id=payment.gateway_payment_id,
                    customer_id=payment.customer_id,
                    amount_paise=payment.amount_paise,
                    method=payment.method,
                    failure_class=classify_failure(payment),
                    failed_at=failed_at,
                    is_holdout=is_holdout(config.seed, holdout_fraction, payment.customer_id),
                )
            )
        return scope

    # -- pulsecover: scheduled detection passes --------------------------

    def _detect(self, db: Session, gt: list[GroundTruthIncident]) -> dict[str, Any]:
        anchor = latest_event_anchor(db)
        if anchor is None:
            return {"passes": 0, "incidents": 0, "precision": None, "recall": None,
                    "f1": None, "mttd_minutes": None, "matched": 0}
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        window = timedelta(minutes=DETECTION_WINDOW_MINUTES)
        step = timedelta(minutes=DETECTION_STEP_MINUTES)
        first_event = db.scalar(sa.select(sa.func.min(PaymentEvent.occurred_at)))
        start = first_event or anchor
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)

        passes = 0
        as_of = start + window
        while as_of <= anchor + step:
            run_detection(
                db,
                DetectionRunRequest(
                    as_of=min(as_of, anchor),
                    window_minutes=DETECTION_WINDOW_MINUTES,
                    # The harness runs the RESEARCH environment: every row in
                    # its scratch DBs is simulator-derived.
                    environment="research",
                ),
            )
            passes += 1
            as_of += step

        incidents = list(db.scalars(sa.select(Incident)))
        matched_gt: set[str] = set()
        true_positive = 0
        mttd_samples: list[float] = []
        for inc in incidents:
            hit = next((g for g in gt if _overlaps(inc, g)), None)
            if hit is None:
                continue
            true_positive += 1
            if hit.entity_id not in matched_gt:
                matched_gt.add(hit.entity_id)
                # Data-time detection latency: the pass window end (the moment
                # the data was visible) minus the injected start.
                seen_at = inc.window_end or inc.detected_at
                mttd_samples.append(max(0.0, (seen_at - hit.start).total_seconds() / 60))
        precision = true_positive / len(incidents) if incidents else None
        recall = len(matched_gt) / len(gt) if gt else None
        f1 = (
            round(2 * precision * recall / (precision + recall), 6)
            if precision is not None and recall is not None and (precision + recall)
            else None
        )
        return {
            "passes": passes,
            "incidents": len(incidents),
            "matched_incidents": true_positive,
            "matched_ground_truth": len(matched_gt),
            "precision": round(precision, 6) if precision is not None else None,
            "recall": round(recall, 6) if recall is not None else None,
            "f1": f1,
            "mttd_minutes": (
                round(sum(mttd_samples) / len(mttd_samples), 2) if mttd_samples else None
            ),
        }

    # -- pulsecover: ML diagnosis vs ground truth ------------------------

    def _diagnose(self, db: Session, gt: list[GroundTruthIncident]) -> dict[str, Any]:
        service = DiagnosisService(db, self._artifacts_dir)
        # Score each ground-truth incident ONCE, via its first-detected
        # matching incident (scheduled passes legitimately re-detect a long
        # incident from later windows; scoring every row would over-weight
        # long incidents). The first detection is what an operator acts on.
        first_match: dict[str, Incident] = {}
        for inc in db.scalars(sa.select(Incident).order_by(Incident.window_end, Incident.id)):
            hit = next((g for g in gt if _overlaps(inc, g)), None)
            if hit is not None and hit.entity_id not in first_match:
                first_match[hit.entity_id] = inc

        top1_hits = 0
        top3_hits = 0
        scored = 0
        per_incident: list[dict[str, Any]] = []
        for gt_id, inc in first_match.items():
            hit = next(g for g in gt if g.entity_id == gt_id)
            try:
                diag = service.classify(inc.id)
            except DiagnosisError as exc:
                per_incident.append({"incident_id": inc.id, "truth": hit.cause, "error": str(exc)})
                continue
            prediction = db.scalar(
                sa.select(ModelPrediction)
                .where(
                    ModelPrediction.incident_id == inc.id,
                    ModelPrediction.prediction_type == "diagnosis",
                )
                .order_by(ModelPrediction.created_at.desc(), ModelPrediction.id.desc())
                .limit(1)
            )
            top3 = [
                t["label"]
                for t in ((prediction.output or {}).get("top3", []) if prediction else [])
            ]
            scored += 1
            top1_hits += int(diag.predicted_cause == hit.cause)
            top3_hits += int(hit.cause in top3)
            per_incident.append(
                {
                    "incident_id": inc.id,
                    "truth": hit.cause,
                    "predicted": diag.predicted_cause,
                    "confidence": diag.confidence,
                    "top3": top3,
                    "correct": diag.predicted_cause == hit.cause,
                }
            )
        return {
            "scored_incidents": scored,
            "top1_accuracy": round(top1_hits / scored, 6) if scored else None,
            "top3_accuracy": round(top3_hits / scored, 6) if scored else None,
            "per_incident": per_incident,
        }

    # -- pulsecover: strategies -> policy gate -> execute -> verify ------

    def _recover(
        self,
        db: Session,
        config: SimulatorConfig,
        gateway: SimulatedPaymentGateway,
        *,
        holdout_fraction: float = 0.0,
    ) -> dict[str, Any]:
        # With a holdout active, opportunities are never built for held-out
        # customers — so no strategies, no actions, no gateway calls reach
        # them. Detection/diagnosis above ran fleet-wide regardless.
        builder: OpportunityBuilder = (
            HoldoutExcludingBuilder(
                db,
                is_excluded=lambda cid: is_holdout(config.seed, holdout_fraction, cid),
            )
            if holdout_fraction > 0.0
            else OpportunityBuilder(db)
        )
        strategies = StrategyGenerator(db)
        executor = RecoveryExecutor(db, gateway)

        policy_outcomes: dict[str, int] = {}
        approvals = 0
        mttr_samples: list[float] = []
        by_type: dict[str, int] = {}

        for inc in db.scalars(sa.select(Incident)):
            built = builder.build_for_incident(inc.id, actor="system:evaluation")
            for opp in built.all:
                strategies.generate(opp)
            db.commit()
            for opp in built.all:
                action = executor.execute(opp.id, actor="agent:evaluation")
                decision = (
                    db.get(PolicyDecisionRecord, action.policy_decision_id)
                    if action.policy_decision_id
                    else None
                )
                policy_outcomes[decision.outcome.value if decision else "NONE"] = (
                    policy_outcomes.get(decision.outcome.value if decision else "NONE", 0) + 1
                )
                if action.status is RecoveryStatus.PENDING_APPROVAL:
                    approvals += 1
                    executor.approve(opp.id, actor=OPERATOR, note="evaluation operator")
                    action = executor.execute(opp.id, actor="agent:evaluation")
                if action.status is RecoveryStatus.VERIFYING:
                    self._customer_resolves(db, config, gateway, action)
                db.commit()

        actions = list(db.scalars(sa.select(RecoveryAction)))
        recovered = 0
        unsafe = 0
        false_interventions = 0
        false_amount = 0
        for action in actions:
            if action.status in (
                RecoveryStatus.PROPOSED,
                RecoveryStatus.REJECTED,
                RecoveryStatus.CANCELLED,
            ):
                continue
            executed = action.status in (
                RecoveryStatus.EXECUTING,
                RecoveryStatus.VERIFYING,
                RecoveryStatus.RECOVERED,
                RecoveryStatus.FAILED,
                RecoveryStatus.UNKNOWN,
            )
            if executed:
                by_type[action.action_type.value] = by_type.get(action.action_type.value, 0) + 1
                # Safety invariant: ALLOWED by the gate, or human-approved.
                decision = (
                    db.get(PolicyDecisionRecord, action.policy_decision_id)
                    if action.policy_decision_id
                    else None
                )
                allowed = decision is not None and decision.outcome is PolicyOutcome.ALLOWED
                if not allowed and not action.approved_by:
                    unsafe += 1
                if action.action_type is ActionType.REFUND:
                    unsafe += 1
                opp = db.get(RecoveryOpportunity, action.opportunity_id)
                if opp and opp.payment_id:
                    payment = db.get(Payment, opp.payment_id)
                    if payment is not None and classify_failure(payment) is FailureClass.HARD_DECLINE:
                        false_interventions += 1
                        false_amount += action.amount_paise
            if action.status is RecoveryStatus.RECOVERED:
                recovered += action.amount_paise
                if action.verified_at and action.proposed_at:
                    mttr_samples.append(
                        max(0.0, (action.verified_at - action.proposed_at).total_seconds() / 60)
                    )

        interventions = sum(by_type.values())
        # Per-type opportunity breakdown (claim-matrix 16.1): the
        # failed-payment vs stuck-checkout vs subscription split, persisted in
        # the run metrics so it is machine-checkable instead of derived during
        # run analysis. Additive: older runs simply lack the key.
        opportunity_types = {
            str(otype): int(count)
            for otype, count in db.execute(
                sa.select(
                    RecoveryOpportunity.opportunity_type, sa.func.count()
                ).group_by(RecoveryOpportunity.opportunity_type)
            )
        }
        return {
            "opportunities_count": int(
                db.scalar(sa.select(sa.func.count()).select_from(RecoveryOpportunity)) or 0
            ),
            "opportunity_types": opportunity_types,
            "actions_count": len(actions),
            "interventions_count": interventions,
            "interventions_by_type": by_type,
            "policy_outcomes": policy_outcomes,
            "approvals_required": approvals,
            "recovered_revenue_paise": recovered,
            "recovered_actions_count": sum(
                1 for a in actions if a.status is RecoveryStatus.RECOVERED
            ),
            "unknown_actions_count": sum(
                1 for a in actions if a.status is RecoveryStatus.UNKNOWN
            ),
            "unsafe_action_count": unsafe,
            "false_interventions_count": false_interventions,
            "false_intervention_amount_paise": false_amount,
            "false_action_rate": (
                round(false_interventions / interventions, 6) if interventions else None
            ),
            "mttr_minutes": (
                round(sum(mttr_samples) / len(mttr_samples), 4) if mttr_samples else None
            ),
        }

    # -- pulsecover: randomized holdout + incremental lift -----------------

    # Action states that reached the gateway (mirrors _recover's `executed`).
    _EXECUTED_STATES = (
        RecoveryStatus.EXECUTING,
        RecoveryStatus.VERIFYING,
        RecoveryStatus.RECOVERED,
        RecoveryStatus.FAILED,
        RecoveryStatus.UNKNOWN,
    )

    def _evaluate_holdout(
        self,
        db: Session,
        config: SimulatorConfig,
        gateway: SimulatedPaymentGateway,
        scope: list[ScopedFailure],
        anchor: datetime | None,
        holdout_fraction: float,
    ) -> dict[str, Any]:
        """Score treatment vs holdout over the run's fixed attribution window.

        BOTH groups share the same organic baseline: a failure the loop never
        touched self-resolves with the documented ``no_action`` prior, and
        the capture is delivered through the REAL signed-webhook path — the
        strict verification standard is identical in both groups (a payment
        without a gateway id can never be verified and is never counted).
        The treatment group additionally gets the loop's executed actions;
        for those payments the action's own conversion draw governs (no
        second organic draw — that would double-count). The lift is therefore
        exactly the causal effect of the actions taken, fleet-wide (ITT):
        every first-attempt failed payment stays in its group's denominator.
        """
        if anchor is not None and anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        if anchor is None and scope:
            anchor = max(f.failed_at for f in scope)

        treatment = [f for f in scope if not f.is_holdout]
        held = [f for f in scope if f.is_holdout]

        action_recovered_ids = set(
            db.scalars(
                sa.select(RecoveryOpportunity.payment_id)
                .join(
                    RecoveryAction,
                    RecoveryAction.opportunity_id == RecoveryOpportunity.id,
                )
                .where(
                    RecoveryAction.status == RecoveryStatus.RECOVERED,
                    RecoveryOpportunity.payment_id.is_not(None),
                )
            )
        )
        executed_payment_ids = set(
            db.scalars(
                sa.select(RecoveryOpportunity.payment_id)
                .join(
                    RecoveryAction,
                    RecoveryAction.opportunity_id == RecoveryOpportunity.id,
                )
                .where(
                    RecoveryAction.status.in_(self._EXECUTED_STATES),
                    RecoveryOpportunity.payment_id.is_not(None),
                )
            )
        )

        # Organic (no-action) baseline for every failure the loop did NOT
        # execute an action against — in both groups.
        organic_treat = self._organic_resolutions(
            db, config, gateway, anchor,
            [f for f in treatment if f.payment_id not in executed_payment_ids],
            namespace="organic",
        )
        organic_hold = self._organic_resolutions(
            db, config, gateway, anchor, held, namespace="holdout",
        )
        db.commit()

        action_recovered = [f for f in treatment if f.payment_id in action_recovered_ids]
        # Sim-time TTR for action recoveries: the batch harness executes at
        # the scenario end, so a verified capture lands at the window's right
        # edge (a disclosed harness artifact; operational latency is the
        # wall-clock MTTR). Organic recoveries use their sampled lag.
        treat_ttr = [
            *(
                max(0.0, (anchor - f.failed_at).total_seconds() / 60)
                for f in action_recovered
            ),
            *(lag for _f, lag in organic_treat),
        ] if anchor is not None else [lag for _f, lag in organic_treat]

        # Isolation proof, counted from the actual rows (the test suite
        # asserts both are 0): nothing built, nothing executed for holdout.
        held_ids = {f.customer_id for f in held if f.customer_id is not None}
        holdout_opps = 0
        holdout_actions = 0
        if held_ids:
            holdout_opps = int(
                db.scalar(
                    sa.select(sa.func.count())
                    .select_from(RecoveryOpportunity)
                    .where(RecoveryOpportunity.customer_id.in_(held_ids))
                )
                or 0
            )
            holdout_actions = int(
                db.scalar(
                    sa.select(sa.func.count())
                    .select_from(RecoveryAction)
                    .join(
                        RecoveryOpportunity,
                        RecoveryAction.opportunity_id == RecoveryOpportunity.id,
                    )
                    .where(RecoveryOpportunity.customer_id.in_(held_ids))
                )
                or 0
            )

        treat_recovered_count = len(action_recovered) + len(organic_treat)
        treat_recovered_amount = (
            sum(f.amount_paise for f in action_recovered)
            + sum(f.amount_paise for f, _lag in organic_treat)
        )
        treat_group = self._group_metrics(
            treatment,
            recovered_count=treat_recovered_count,
            recovered_amount=treat_recovered_amount,
            ttr_samples=treat_ttr,
        )
        hold_group = self._group_metrics(
            held,
            recovered_count=len(organic_hold),
            recovered_amount=sum(f.amount_paise for f, _lag in organic_hold),
            ttr_samples=[lag for _f, lag in organic_hold],
        )
        lift_low, lift_high = newcombe_ci(
            treat_recovered_count,
            len(treatment),
            len(organic_hold),
            len(held),
        )
        lift_point = treat_group["recovery_rate"] - hold_group["recovery_rate"]
        window_hours = (
            round((anchor - min(f.failed_at for f in scope)).total_seconds() / 3600, 2)
            if anchor is not None and scope
            else None
        )
        scoped_customer_ids = {f.customer_id for f in scope if f.customer_id is not None}
        treat_recovered_ids = action_recovered_ids | {
            f.payment_id for f, _lag in organic_treat
        }
        hold_recovered_ids = {f.payment_id for f, _lag in organic_hold}
        strata_by_class = self._strata(
            treatment, held, treat_recovered_ids, hold_recovered_ids,
            key=lambda f: f.failure_class.value,
        )
        strata_by_method = self._strata(
            treatment, held, treat_recovered_ids, hold_recovered_ids,
            key=lambda f: f.method or "unknown",
        )
        return {
            "configured_fraction": holdout_fraction,
            "realized_fraction": (
                round(len(held_ids) / len(scoped_customer_ids), 4)
                if scoped_customer_ids
                else 0.0
            ),
            "seed": config.seed,
            "assignment": "customer-level; sha256('holdout:{seed}:{customer_id}') "
            "first 8 bytes / 2^64 < fraction — deterministic per run seed",
            "estimand": "incremental lift = recovery_rate(treatment) - "
            "recovery_rate(holdout); denominators are ALL first-attempt "
            "failed payments per group; recovery = gateway/webhook-verified "
            "captures only; both groups share the organic no_action baseline",
            "attribution_window": {
                "start": "each payment's failure timestamp (created_at)",
                "end": "scenario-end anchor (latest terminal simulator event)",
                "max_window_hours": window_hours,
            },
            "ci_method": "newcombe_hybrid_score_wilson_95",
            "customers": {
                "treatment": len(
                    {f.customer_id for f in treatment if f.customer_id is not None}
                ),
                "holdout": len(held_ids),
            },
            "treatment": {
                **treat_group,
                "recovered_via_action": len(action_recovered),
                "recovered_organic": len(organic_treat),
            },
            "holdout": hold_group,
            "lift": {
                "point": round(lift_point, 6),
                "ci95_low": round(lift_low, 6),
                "ci95_high": round(lift_high, 6),
            },
            "lift_class_adjusted": self._standardized_lift(strata_by_class),
            "strata": {
                "by_failure_class": strata_by_class,
                "by_method": strata_by_method,
            },
            "isolation": {
                "holdout_opportunities_count": holdout_opps,
                "holdout_actions_count": holdout_actions,
            },
            "notes": [
                "Held-out customers still get detection + diagnosis; only the "
                "recovery loop (opportunities, actions, gateway calls) is "
                "withheld — a no-action control, not a blind spot.",
                "Both groups share the organic no_action baseline (the same "
                "documented prior family as the action conversion table): "
                "failures the loop never executed an action against "
                "self-resolve with the no_action probability, captured "
                "through the real signed-webhook path in both groups.",
                "Payments with an executed action resolve on the action's own "
                "conversion draw only — no second organic draw.",
                "Self-resolution lags sampled past the scenario-end anchor "
                "are right-censored (counted as not recovered).",
                "Sim-time action time-to-recovery is a batch artifact: the "
                "harness executes all actions at the scenario end. The "
                "operational latency measure is wall-clock MTTR.",
                "lift_class_adjusted removes chance failure-class imbalance "
                "between the randomized groups (pooled-weight post-"
                "stratification on the pre-registered stratification "
                "factor); the raw ITT contrast above stays primary.",
            ],
        }

    @staticmethod
    def _standardized_lift(strata: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Mix-adjusted lift over the pre-registered failure-class strata:
        the pooled-weight sum of per-stratum contrasts (post-stratification —
        removes chance class-mix imbalance between the randomized groups).
        CI: normal approximation with var = Σ w_i²·(p_t(1−p_t)/n_t +
        p_h(1−p_h)/n_h). Documented as the secondary estimator; the raw ITT
        contrast with its Newcombe/Wilson CI stays the primary one."""
        total = sum(
            s["treatment"]["failed_payments"] + s["holdout"]["failed_payments"]
            for s in strata
        )
        if not total:
            return None
        point = 0.0
        var = 0.0
        for s in strata:
            n_t = s["treatment"]["failed_payments"]
            n_h = s["holdout"]["failed_payments"]
            w = (n_t + n_h) / total
            p_t = s["treatment"]["recovery_rate"]
            p_h = s["holdout"]["recovery_rate"]
            point += w * (p_t - p_h)
            var += w * w * (
                (p_t * (1 - p_t) / n_t if n_t else 0.0)
                + (p_h * (1 - p_h) / n_h if n_h else 0.0)
            )
        half = CI_Z * math.sqrt(var)
        return {
            "point": round(point, 6),
            "ci95_low": round(max(-1.0, point - half), 6),
            "ci95_high": round(min(1.0, point + half), 6),
            "method": "pooled-weight post-stratification over failure-class "
            "strata; normal-approximation CI",
        }

    def _organic_resolutions(
        self,
        db: Session,
        config: SimulatorConfig,
        gateway: SimulatedPaymentGateway,
        anchor: datetime | None,
        failures: list[ScopedFailure],
        *,
        namespace: str,
    ) -> list[tuple[ScopedFailure, float]]:
        """The no-action counterfactual for a set of failed payments: each
        self-resolves with the documented prior and a uniform (0, 7d] lag,
        right-censored at the window end; captures are delivered through the
        real signed-webhook path (verified, dedup-safe). Deterministic per
        (seed, payment id)."""
        recovered: list[tuple[ScopedFailure, float]] = []
        for f in failures:
            rng = random.Random(f"eval:{config.seed}:{namespace}:{f.payment_id}")
            if rng.random() >= CONVERSION[f.failure_class]["no_action"]:
                continue  # the customer never comes back on their own
            lag_minutes = rng.random() * SELF_RESOLUTION_MAX_LAG_MINUTES
            if anchor is not None and f.failed_at + timedelta(minutes=lag_minutes) > anchor:
                continue  # right-censored at the window end
            if not f.gateway_payment_id:
                continue  # no gateway id -> capture could never be verified
            entity = {
                "id": f.gateway_payment_id,
                "entity": "payment",
                "amount": f.amount_paise,
                "currency": "INR",
                "status": "captured",
                "captured": True,
                "method": f.method,
            }
            self._deliver_webhook(db, gateway, "payment.captured", entity)
            recovered.append((f, lag_minutes))
        return recovered

    @staticmethod
    def _group_metrics(
        group: list[ScopedFailure],
        *,
        recovered_count: int,
        recovered_amount: int,
        ttr_samples: list[float],
    ) -> dict[str, Any]:
        failed_amount = sum(f.amount_paise for f in group)
        return {
            "failed_payments": len(group),
            "failed_amount_paise": failed_amount,
            "recovered_payments": recovered_count,
            "recovered_amount_paise": recovered_amount,
            "recovery_rate": round(recovered_count / len(group), 6) if group else 0.0,
            "recovery_rate_amount": (
                round(recovered_amount / failed_amount, 6) if failed_amount else 0.0
            ),
            "median_time_to_recover_minutes": (
                round(med, 2) if (med := median(ttr_samples)) is not None else None
            ),
        }

    @staticmethod
    def _strata(
        treatment: list[ScopedFailure],
        held: list[ScopedFailure],
        treat_recovered_ids: set[str],
        hold_recovered_ids: set[str],
        *,
        key: Any,
    ) -> list[dict[str, Any]]:
        """Per-stratum lift with the same Newcombe/Wilson CI — tiny strata
        report honestly wide bands, never bare point estimates."""
        names = sorted({key(f) for f in treatment} | {key(f) for f in held})
        rows: list[dict[str, Any]] = []
        for name in names:
            t_group = [f for f in treatment if key(f) == name]
            h_group = [f for f in held if key(f) == name]
            t_ok = sum(1 for f in t_group if f.payment_id in treat_recovered_ids)
            h_ok = sum(1 for f in h_group if f.payment_id in hold_recovered_ids)
            t_rate = t_ok / len(t_group) if t_group else 0.0
            h_rate = h_ok / len(h_group) if h_group else 0.0
            low, high = newcombe_ci(t_ok, len(t_group), h_ok, len(h_group))
            rows.append(
                {
                    "stratum": name,
                    "treatment": {
                        "failed_payments": len(t_group),
                        "recovered_payments": t_ok,
                        "recovery_rate": round(t_rate, 6),
                    },
                    "holdout": {
                        "failed_payments": len(h_group),
                        "recovered_payments": h_ok,
                        "recovery_rate": round(h_rate, 6),
                    },
                    "lift": {
                        "point": round(t_rate - h_rate, 6),
                        "ci95_low": round(low, 6),
                        "ci95_high": round(high, 6),
                    },
                }
            )
        rows.sort(key=lambda r: -r["treatment"]["failed_payments"])
        return rows

    def _customer_resolves(
        self,
        db: Session,
        config: SimulatorConfig,
        gateway: SimulatedPaymentGateway,
        action: RecoveryAction,
    ) -> None:
        """The simulated customer answers a VERIFYING action, deterministically
        per gateway_request_id; verification itself runs the real code paths."""
        opp = db.get(RecoveryOpportunity, action.opportunity_id)
        payment = db.get(Payment, opp.payment_id) if opp and opp.payment_id else None
        cls = classify_failure(payment) if payment is not None else FailureClass.UNKNOWN
        column = "notify"
        if action.action_type is ActionType.RETRY_PAYMENT:
            delay = 0
            if action.strategy_id:
                strategy = db.get(RecoveryStrategy, action.strategy_id)
                try:
                    delay = int((strategy.constraints or {}).get("delay_seconds", 0)) if strategy else 0
                except (TypeError, ValueError):
                    delay = 0
            column = "delayed_retry" if delay > 0 else "immediate_retry"
        rng = random.Random(
            f"eval:{config.seed}:pulsecover:{_stable_subject(opp, payment, action)}"
        )
        pays = rng.random() < CONVERSION[cls][column]

        if action.action_type in (ActionType.RETRY_PAYMENT, ActionType.NOTIFY_CUSTOMER):
            if not pays or payment is None or not payment.gateway_payment_id:
                return  # the customer never completes the payment
            # The simulated customer pays after the retry/nudge. Verification
            # uses the system's real path: a signed payment.captured webhook
            # for the original payment (the reconciler links it to the action
            # via recovery_opportunities.payment_id).
            entity = {
                "id": payment.gateway_payment_id,
                "entity": "payment",
                "amount": payment.amount_paise,
                "currency": payment.currency or "INR",
                "status": "captured",
                "captured": True,
                "method": payment.method,
            }
            self._deliver_webhook(db, gateway, "payment.captured", entity)
        # CREATE_PAYMENT_LINK is decided by the twin inline; nothing to do.

    @staticmethod
    def _deliver_webhook(
        db: Session, gateway: SimulatedPaymentGateway, event_type: str, entity: dict
    ) -> None:
        """Feed a signed simulator webhook through the real handler registry
        (dedup on gateway_event_id included) — the same code the HTTP
        endpoint dispatches to."""
        body, _signature, event_id = gateway.build_event(event_type, entity)
        payload = json.loads(body)
        row = WebhookEvent(
            gateway_event_id=event_id,
            event_type=event_type,
            payload=payload,
            signature_valid=True,
            processed=False,
            received_at=utcnow(),
            source="simulator",
        )
        db.add(row)
        try:
            db.flush()
        except sa.exc.IntegrityError:
            db.rollback()
            return  # duplicate delivery: zero side effects, as the endpoint does
        handler = EVENT_HANDLERS.get(event_type)
        if handler is not None:
            row.error = handler(db, payload)
        row.processed = True
        row.processed_at = utcnow()
        db.flush()


__all__ = [
    "CONVERSION",
    "DETECTION_STEP_MINUTES",
    "DETECTION_WINDOW_MINUTES",
    "EvaluationRunner",
    "GATEWAY_SUCCESS_RATE",
    "KIND_TO_CAUSE",
    "OPERATOR",
    "SELF_RESOLUTION_MAX_LAG_MINUTES",
    "ScopedFailure",
    "dataset_version",
    "resolve_anchor",
    "truth_cause",
]

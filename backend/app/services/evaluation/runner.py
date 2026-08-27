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
    (failure-class x action) conversion table, seeded deterministically per
    gateway_request_id; payment links are decided by the gateway twin itself.
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

import dataclasses
import json
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
from app.services.diagnosis.service import DiagnosisError, DiagnosisService
from app.services.diagnosis.taxonomy import CauseLabel
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
CONVERSION: dict[FailureClass, dict[str, float]] = {
    FailureClass.TIMEOUT: {
        "immediate_retry": 0.55,
        "delayed_retry": 0.50,
        "payment_link": 0.50,
        "notify": 0.30,
    },
    FailureClass.SOFT_DECLINE: {
        "immediate_retry": 0.30,
        "delayed_retry": 0.35,
        "payment_link": 0.35,
        "notify": 0.20,
    },
    FailureClass.INSUFFICIENT_FUNDS: {
        "immediate_retry": 0.08,
        "delayed_retry": 0.35,
        "payment_link": 0.20,
        "notify": 0.15,
    },
    FailureClass.ABANDONMENT: {
        "immediate_retry": 0.15,
        "delayed_retry": 0.12,
        "payment_link": 0.30,
        "notify": 0.20,
    },
    FailureClass.HARD_DECLINE: {
        "immediate_retry": 0.02,
        "delayed_retry": 0.02,
        "payment_link": 0.03,
        "notify": 0.02,
    },
    FailureClass.UNKNOWN: {
        "immediate_retry": 0.15,
        "delayed_retry": 0.15,
        "payment_link": 0.18,
        "notify": 0.10,
    },
}

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


def _parse_ts(value: Any) -> datetime:
    ts = datetime.fromisoformat(str(value))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


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
    ) -> EvaluationRun:
        if scenario not in SCENARIOS:
            raise ValueError(
                f"unknown scenario {scenario!r}; known: {', '.join(sorted(SCENARIOS))}"
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
            }.items()
            if v is not None
        }
        config = dataclasses.replace(base, **overrides)

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
                "base_config": config.config_dict(),
                "detection": {
                    "step_minutes": DETECTION_STEP_MINUTES,
                    "window_minutes": DETECTION_WINDOW_MINUTES,
                    "detector": "production defaults",
                },
                "gateway_success_rate": GATEWAY_SUCCESS_RATE,
                "conversion_model": {
                    k.value: v for k, v in CONVERSION.items()
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
            pulsecover = self._run_pulsecover(config)
            metrics = self._assemble(baseline, pulsecover)
            run.status = "completed"
            run.metrics = metrics
            run.finished_at = utcnow()
            run.notes = (
                "Arms ran in isolated scratch DBs; simulator_run_ids are in "
                "metrics.arms.*.simulator_run_id. MTTD is simulator-time; "
                "MTTR is wall-clock pipeline latency."
            )
            experiment.status = "completed"
            experiment.ended_at = run.finished_at
            experiment.results = {
                "comparison": metrics.get("comparison", {}),
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
        return {
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

    def _run_pulsecover(self, config: SimulatorConfig) -> ArmResult:
        with _ScratchDb() as scratch:
            db = scratch.session
            sim = run_simulation(config, db)
            gateway = SimulatedPaymentGateway(
                seed=config.seed, success_rate=GATEWAY_SUCCESS_RATE
            )
            gt = load_ground_truth(db, sim.run_id)

            detection = self._detect(db, gt)
            diagnosis = self._diagnose(db, gt)
            recovery = self._recover(db, config, gateway)

            failed = list(
                db.scalars(sa.select(Payment).where(Payment.status == "failed"))
            )
            failed_amount = sum(p.amount_paise for p in failed)
            metrics: dict[str, Any] = {
                "simulator_run_id": sim.run_id,
                "ground_truth_incidents": len(gt),
                "detection": detection,
                "diagnosis": diagnosis,
                **recovery,
                "failed_payments_count": len(failed),
                "failed_amount_paise": failed_amount,
            }
            rec = recovery["recovered_revenue_paise"]
            metrics["recovery_rate"] = (
                round(rec / failed_amount, 6) if failed_amount else 0.0
            )
            return ArmResult(arm="pulsecover", simulator_run_id=sim.run_id, metrics=metrics)

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
                    as_of=min(as_of, anchor), window_minutes=DETECTION_WINDOW_MINUTES
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
        self, db: Session, config: SimulatorConfig, gateway: SimulatedPaymentGateway
    ) -> dict[str, Any]:
        builder = OpportunityBuilder(db)
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
        return {
            "opportunities_count": int(
                db.scalar(sa.select(sa.func.count()).select_from(RecoveryOpportunity)) or 0
            ),
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
        rng = random.Random(f"eval:{config.seed}:pulsecover:{action.gateway_request_id}")
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
    "truth_cause",
]

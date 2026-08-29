"""Demo endpoints — judge-facing scenario triggers and environment reset.

Exempt from the X-API-Key requirement outside prod (see app.main) so judges can
reset and trigger scenarios from the UI.

Scenario trigger = idempotent simulator run + ONE anchored detection pass.
The pass is anchored so the scenario's latest injected incident sits inside the
analysis window (as_of ~= incident end, window = duration + 90min of baseline,
capped at the 24h request limit). A second POST of the same scenario skips the
seed (deterministic run id) and the detection pass UPSERTs the same incidents —
no duplicates, ever.

Reset clears simulator-generated commerce rows and the RESEARCH derived rows
(incidents/evidence/diagnoses/opportunities/strategies/actions/
policy_decisions/agent_reports plus 'simulator'-sourced webhook_events). It
deliberately KEEPS evaluation_runs, experiments and model_predictions — the
scientific record a demo reset must not rewrite — and every real_test row
(Razorpay Test Mode data is untouchable by reset). It appends exactly ONE
audit_logs row (environment 'research') recording what was cleared (their
incident/simulator references may dangle afterwards; that is accepted and
documented in docs/evaluation.md).
"""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.logging import get_logger, request_id_ctx
from app.models import (
    AgentReport,
    Customer,
    Diagnosis,
    Incident,
    IncidentEvidence,
    Merchant,
    Order,
    Payment,
    PaymentEvent,
    PolicyDecisionRecord,
    RecoveryAction,
    RecoveryOpportunity,
    RecoveryStrategy,
    SimulatorGroundTruth,
    SimulatorRun,
    Subscription,
    WebhookEvent,
)
from app.schemas.demo import (
    DemoResetResponse,
    ScenarioInfo,
    ScenarioListResponse,
    ScenarioTriggerResponse,
)
from app.schemas.detection import DetectionRunRequest
from app.services.detection import run_detection
from app.services.policy import audit
from app.models.base import ENVIRONMENT_RESEARCH, SOURCE_TYPE_SIMULATOR
from app.simulator import SCENARIOS, list_scenarios
from app.simulator.cli import run_idempotent
from app.simulator.engine import SimResult

logger = get_logger("app.api.v1.demo")

router = APIRouter(prefix="/api/v1/demo", tags=["demo"])

# Bulk-delete order respects foreign keys without relying on PRAGMA
# foreign_keys (SQLite) — children first, parents last. policy_decisions is
# deleted BEFORE recovery_actions: its environment scoping reads the linked
# actions (soft reference), and the real FK recovery_actions.policy_decision_id
# is declared ON DELETE SET NULL, so removing decisions first is safe.
_RESET_TABLES: list[tuple[str, Any]] = [
    ("policy_decisions", PolicyDecisionRecord),
    ("recovery_actions", RecoveryAction),
    ("recovery_strategies", RecoveryStrategy),
    ("recovery_opportunities", RecoveryOpportunity),
    ("agent_reports", AgentReport),
    ("diagnoses", Diagnosis),
    ("incident_evidence", IncidentEvidence),
    ("incidents", Incident),
    ("webhook_events", WebhookEvent),
    ("payment_events", PaymentEvent),
    ("payments", Payment),
    ("orders", Order),
    ("subscriptions", Subscription),
    ("customers", Customer),
    ("merchants", Merchant),
    ("simulator_ground_truth", SimulatorGroundTruth),
    ("simulator_runs", SimulatorRun),
]
_KEPT_TABLES = ["evaluation_runs", "experiments", "model_predictions", "audit_logs"]

# Demo reset is pinned to the RESEARCH environment: it deletes ONLY
# simulator-sourced commerce rows and 'research'-environment derived rows.
# real_test rows (Razorpay Test Mode data) are untouchable by reset.
_ENV_DERIVED_TABLES = {
    "recovery_actions",
    "recovery_opportunities",
    "agent_reports",
    "diagnoses",
    "incident_evidence",
    "incidents",
}
_SIMULATOR_COMMERCE_TABLES = {
    "payment_events",
    "payments",
    "orders",
    "subscriptions",
    "customers",
    "merchants",
}


def _reset_statement(table: str, model: Any) -> Any:
    """The environment-scoped DELETE for one reset table (see above)."""
    if table in _ENV_DERIVED_TABLES:
        return sa.delete(model).where(model.environment == ENVIRONMENT_RESEARCH)
    if table in _SIMULATOR_COMMERCE_TABLES:
        return sa.delete(model).where(model.source_type == SOURCE_TYPE_SIMULATOR)
    if table == "recovery_strategies":
        # No environment column: scope via the parent opportunity.
        research_opps = sa.select(RecoveryOpportunity.id).where(
            RecoveryOpportunity.environment == ENVIRONMENT_RESEARCH
        )
        return sa.delete(RecoveryStrategy).where(
            RecoveryStrategy.opportunity_id.in_(research_opps)
        )
    if table == "policy_decisions":
        # No environment column: scope via the linked action. Decisions with
        # no action link (plan previews) are demo-time evaluations and are
        # cleared as before; action-linked real_test decisions survive.
        research_actions = sa.select(RecoveryAction.id).where(
            RecoveryAction.environment == ENVIRONMENT_RESEARCH
        )
        return sa.delete(PolicyDecisionRecord).where(
            sa.or_(
                PolicyDecisionRecord.action_id.is_(None),
                PolicyDecisionRecord.action_id.in_(research_actions),
            )
        )
    if table == "webhook_events":
        # The intake stamp IS the environment boundary: 'simulator' deliveries
        # are research; 'razorpay' deliveries are real_test evidence.
        return sa.delete(WebhookEvent).where(WebhookEvent.source == "simulator")
    # simulator_runs / simulator_ground_truth are research-owned by definition.
    return sa.delete(model)


@router.get("/scenarios", response_model=ScenarioListResponse)
def get_scenarios() -> ScenarioListResponse:
    return ScenarioListResponse(
        scenarios=[ScenarioInfo(**s) for s in list_scenarios()]
    )


@router.post("/scenario/{name}", response_model=ScenarioTriggerResponse)
def trigger_scenario(name: str, db: Session = Depends(get_db)) -> ScenarioTriggerResponse:
    if name not in SCENARIOS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown scenario {name!r}; known: {', '.join(sorted(SCENARIOS))}",
        )
    factory = SCENARIOS[name][1]
    config = factory()  # type: ignore[operator]

    try:
        result: SimResult = run_idempotent(db, config, force=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    gt_rows = list(
        db.scalars(
            sa.select(SimulatorGroundTruth).where(
                SimulatorGroundTruth.simulator_run_id == result.run_id,
                SimulatorGroundTruth.entity_type == "incident",
            )
        )
    )

    detection_summary: dict[str, Any] | None = None
    first_incident_id: str | None = None
    try:
        detection_summary, first_incident_id = _anchored_detection_pass(db, gt_rows)
    except ValueError as exc:
        logger.warning("demo detection pass failed: %s", exc)
        detection_summary = {"status": "failed", "detail": str(exc)}

    return ScenarioTriggerResponse(
        scenario=config.scenario,
        status="completed",
        simulator_run_id=result.run_id,
        incident_id=first_incident_id,
        detail=(
            "identical run already seeded; detection re-confirmed the existing incidents"
            if result.skipped
            else f"seeded {config.scenario} and ran one anchored detection pass"
        ),
        skipped=result.skipped,
        stats={k: v for k, v in (result.stats or {}).items() if k != "incidents"},
        detection=detection_summary,
    )


def _anchored_detection_pass(
    db: Session, gt_rows: list[SimulatorGroundTruth]
) -> tuple[dict[str, Any], str | None]:
    """One detection pass anchored at the latest ground-truth incident so the
    injected anomaly is guaranteed inside the analysis window. Demo scenarios
    are simulator-seeded: the pass is pinned to the RESEARCH environment."""
    req = DetectionRunRequest(environment="research")
    if gt_rows:
        def _end(row: SimulatorGroundTruth) -> datetime:
            return datetime.fromisoformat(str(row.truth["end"]))

        def _start(row: SimulatorGroundTruth) -> datetime:
            return datetime.fromisoformat(str(row.truth["start"]))

        latest = max(gt_rows, key=_end)
        duration_min = int((_end(latest) - _start(latest)).total_seconds() // 60)
        req = DetectionRunRequest(
            as_of=_end(latest),
            window_minutes=min(24 * 60, max(60, duration_min + 90)),
            environment="research",
        )
    result = run_detection(db, req)
    first = next(
        (i for i in result.incidents if i.action == "created"), None
    ) or (result.incidents[0] if result.incidents else None)
    summary = {
        "run_id": result.run_id,
        "status": result.status,
        "anomalies_detected": result.anomalies_detected,
        "incidents_created": result.incidents_created,
        "incidents_updated": result.incidents_updated,
        "detail": result.detail,
        "incidents": [
            {
                "incident_id": i.incident_id,
                "action": i.action,
                "metric": i.metric,
                "severity": i.severity.value,
                "deviation_pct": i.deviation_pct,
                "revenue_at_risk_paise": i.revenue_at_risk_paise,
            }
            for i in result.incidents
        ],
    }
    return summary, first.incident_id if first else None


@router.post("/reset", response_model=DemoResetResponse)
def reset_demo(db: Session = Depends(get_db)) -> DemoResetResponse:
    cleared: dict[str, int] = {}
    for table, model in _RESET_TABLES:
        cleared[table] = int(db.execute(_reset_statement(table, model)).rowcount or 0)
    entry = audit.record(
        db,
        actor="system:demo",
        action="demo.reset",
        entity_type="demo_environment",
        entity_id="demo",
        details={"cleared": cleared, "kept": list(_KEPT_TABLES)},
        request_id=request_id_ctx.get(),
    )
    # The reset audit row is research-tagged: it can never appear in a
    # real_test audit query (the ORM default is already 'research'; explicit
    # here because this row IS the environment boundary's self-record).
    entry.environment = ENVIRONMENT_RESEARCH
    db.commit()
    logger.info("demo environment reset", extra={"cleared": cleared})
    return DemoResetResponse(
        status="ok",
        cleared=cleared,
        reset_at=utcnow(),
        kept=list(_KEPT_TABLES),
        audit_id=entry.id,
    )

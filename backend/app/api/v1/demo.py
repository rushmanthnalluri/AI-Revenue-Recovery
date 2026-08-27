"""Demo endpoints — judge-facing scenario triggers and environment reset.

Exempt from the X-API-Key requirement outside prod (see app.main) so judges can
reset and trigger scenarios from the UI.

Scenario trigger = idempotent simulator run + ONE anchored detection pass.
The pass is anchored so the scenario's latest injected incident sits inside the
analysis window (as_of ~= incident end, window = duration + 90min of baseline,
capped at the 24h request limit). A second POST of the same scenario skips the
seed (deterministic run id) and the detection pass UPSERTs the same incidents —
no duplicates, ever.

Reset clears ALL simulator-generated commerce rows and every derived table
(incidents/evidence/diagnoses/opportunities/strategies/actions/
policy_decisions/agent_reports/webhook_events). It deliberately KEEPS
evaluation_runs, experiments and model_predictions — the scientific record a
demo reset must not rewrite — and appends exactly ONE audit_logs row recording
what was cleared (their incident/simulator references may dangle afterwards;
that is accepted and documented in docs/evaluation.md).
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
from app.simulator import SCENARIOS, list_scenarios
from app.simulator.cli import run_idempotent
from app.simulator.engine import SimResult

logger = get_logger("app.api.v1.demo")

router = APIRouter(prefix="/api/v1/demo", tags=["demo"])

# Bulk-delete order respects foreign keys without relying on PRAGMA
# foreign_keys (SQLite) — children first, parents last.
_RESET_TABLES: list[tuple[str, Any]] = [
    ("recovery_actions", RecoveryAction),
    ("recovery_strategies", RecoveryStrategy),
    ("policy_decisions", PolicyDecisionRecord),
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
    injected anomaly is guaranteed inside the analysis window."""
    req = DetectionRunRequest()
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
        cleared[table] = int(db.execute(sa.delete(model)).rowcount or 0)
    entry = audit.record(
        db,
        actor="system:demo",
        action="demo.reset",
        entity_type="demo_environment",
        entity_id="demo",
        details={"cleared": cleared, "kept": list(_KEPT_TABLES)},
        request_id=request_id_ctx.get(),
    )
    db.commit()
    logger.info("demo environment reset", extra={"cleared": cleared})
    return DemoResetResponse(
        status="ok",
        cleared=cleared,
        reset_at=utcnow(),
        kept=list(_KEPT_TABLES),
        audit_id=entry.id,
    )

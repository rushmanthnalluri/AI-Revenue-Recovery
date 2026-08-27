"""Evaluation endpoints — stored-row reads + synchronous run trigger.

POST /run executes the experiment SYNCHRONOUSLY: two simulator arms
(baseline + PulseRecover) in isolated scratch SQLite databases, then one
evaluation_runs row (+ one experiments row) persisted here. At the default
scale (10 days / 12k events per arm) a run takes well under a minute; the
synchronous contract keeps the demo story simple and is documented in
docs/evaluation.md. GET endpoints serve STORED rows only — they never
compute metrics on the fly.
"""

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import EvaluationRun, Experiment
from app.schemas.evaluation import (
    EvaluationMetrics,
    EvaluationRunDetail,
    EvaluationRunListResponse,
    EvaluationRunSummary,
    RunEvaluationRequest,
    RunEvaluationResponse,
)
from app.services.evaluation import EvaluationRunner

router = APIRouter(prefix="/api/v1/evaluation", tags=["evaluation"])


def _summary(run: EvaluationRun) -> EvaluationRunSummary:
    return EvaluationRunSummary(
        id=run.id,
        name=run.name,
        evaluation_type=run.evaluation_type,
        dataset=run.dataset,
        simulator_run_id=run.simulator_run_id,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


@router.get("/runs", response_model=EvaluationRunListResponse)
def list_runs(
    db: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> EvaluationRunListResponse:
    total = int(
        db.scalar(sa.select(sa.func.count()).select_from(EvaluationRun)) or 0
    )
    rows = db.scalars(
        sa.select(EvaluationRun)
        .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return EvaluationRunListResponse(
        items=[_summary(r) for r in rows], total=total, page=page, page_size=page_size
    )


@router.get("/runs/{run_id}", response_model=EvaluationRunDetail)
def get_run(run_id: str, db: Session = Depends(get_db)) -> EvaluationRunDetail:
    run = db.get(EvaluationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"evaluation run not found: {run_id!r}")
    return EvaluationRunDetail(
        **_summary(run).model_dump(),
        metrics=dict(run.metrics or {}),
        notes=run.notes,
    )


@router.get("/metrics", response_model=EvaluationMetrics)
def get_metrics(db: Session = Depends(get_db)) -> EvaluationMetrics:
    """Aggregate over COMPLETED stored runs (means of available values)."""
    runs = list(
        db.scalars(sa.select(EvaluationRun).where(EvaluationRun.status == "completed"))
    )
    out = EvaluationMetrics(runs_count=len(runs))

    def _mean(key: str) -> float | None:
        vals = [
            run.metrics[key]
            for run in runs
            if isinstance((run.metrics or {}).get(key), (int, float))
        ]
        return round(sum(vals) / len(vals), 6) if vals else None

    out.detection_precision = _mean("detection_precision")
    out.detection_recall = _mean("detection_recall")
    out.detection_f1 = _mean("detection_f1")
    out.diagnosis_top1_accuracy = _mean("diagnosis_top1_accuracy")
    out.diagnosis_top3_accuracy = _mean("diagnosis_top3_accuracy")
    out.mean_time_to_detect_minutes = _mean("mean_time_to_detect_minutes")
    out.mean_time_to_recover_minutes = _mean("mean_time_to_recover_minutes")
    out.recovery_rate = _mean("recovery_rate")
    out.false_action_rate = _mean("false_action_rate")
    out.baseline_recovery_rate = _mean("baseline_recovery_rate")
    out.recovered_revenue_paise = sum(
        int((run.metrics or {}).get("recovered_revenue_paise") or 0) for run in runs
    )
    out.baseline_recovered_revenue_paise = sum(
        int((run.metrics or {}).get("baseline_recovered_revenue_paise") or 0)
        for run in runs
    )
    out.unsafe_action_count = sum(
        int((run.metrics or {}).get("unsafe_action_count") or 0) for run in runs
    )
    return out


@router.post("/run", response_model=RunEvaluationResponse)
def run_evaluation(
    body: RunEvaluationRequest, db: Session = Depends(get_db)
) -> RunEvaluationResponse:
    scenario = body.scenario or (
        body.dataset if body.dataset != "simulator" else "standard"
    )
    runner = EvaluationRunner(db)
    try:
        run = runner.run(
            name=body.name,
            scenario=scenario,
            seed=body.seed,
            days=body.days,
            events=body.events,
            customers=body.customers,
            evaluation_type=body.evaluation_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # The failed run row (status=failed, notes) is already persisted.
        raise HTTPException(
            status_code=500, detail=f"evaluation run failed: {type(exc).__name__}: {exc}"
        ) from exc
    experiment_id = db.scalar(
        sa.select(Experiment.id).where(Experiment.name == f"{body.name}:{run.id}")
    )
    return RunEvaluationResponse(
        run_id=run.id,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        experiment_id=experiment_id,
        metrics=dict(run.metrics or {}),
    )

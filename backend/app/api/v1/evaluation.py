"""Evaluation endpoints. Owner: evaluation/simulator agent."""

from fastapi import APIRouter

from app.api import not_implemented
from app.schemas.evaluation import (
    EvaluationMetrics,
    EvaluationRunListResponse,
    RunEvaluationRequest,
)
from fastapi import Query

router = APIRouter(prefix="/api/v1/evaluation", tags=["evaluation"])


@router.get("/runs", response_model=EvaluationRunListResponse)
def list_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> EvaluationRunListResponse:
    return EvaluationRunListResponse(items=[], total=0, page=page, page_size=page_size)


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    # 501 stub: response shape is app.schemas.evaluation.EvaluationRunDetail.
    return not_implemented("evaluation run detail")


@router.get("/metrics", response_model=EvaluationMetrics)
def get_metrics() -> EvaluationMetrics:
    return EvaluationMetrics()


@router.post("/run")
def run_evaluation(body: RunEvaluationRequest):
    # 501 stub: response shape is app.schemas.evaluation.RunEvaluationResponse.
    return not_implemented("run evaluation")

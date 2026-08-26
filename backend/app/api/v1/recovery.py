"""Recovery endpoints. Owner: recovery/policy agent.

Every mutating endpoint ultimately flows through the deterministic policy gate
before any gateway call is made."""

from fastapi import APIRouter, Query

from app.api import not_implemented
from app.ports import RecoveryStatus
from app.schemas.recovery import (
    ApproveRequest,
    CancelRequest,
    EscalateRequest,
    ExecuteRequest,
    OpportunityListResponse,
    RejectRequest,
)

router = APIRouter(prefix="/api/v1/recovery", tags=["recovery"])


@router.get("/opportunities", response_model=OpportunityListResponse)
def list_opportunities(
    status: RecoveryStatus | None = Query(default=None),
    incident_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> OpportunityListResponse:
    return OpportunityListResponse(items=[], total=0, page=page, page_size=page_size)


@router.get("/{opportunity_id}")
def get_opportunity(opportunity_id: str):
    # 501 stub: response shape is app.schemas.recovery.OpportunityDetail.
    return not_implemented("opportunity detail")


@router.get("/{opportunity_id}/plan")
def get_plan(opportunity_id: str):
    # 501 stub: response shape is app.schemas.recovery.RecoveryPlan.
    return not_implemented("recovery plan")


@router.post("/{opportunity_id}/approve")
def approve(opportunity_id: str, body: ApproveRequest):
    return not_implemented("approve recovery")


@router.post("/{opportunity_id}/reject")
def reject(opportunity_id: str, body: RejectRequest):
    return not_implemented("reject recovery")


@router.post("/{opportunity_id}/escalate")
def escalate(opportunity_id: str, body: EscalateRequest):
    return not_implemented("escalate recovery")


@router.post("/{opportunity_id}/execute")
def execute(opportunity_id: str, body: ExecuteRequest):
    return not_implemented("execute recovery")


@router.post("/{opportunity_id}/cancel")
def cancel(opportunity_id: str, body: CancelRequest):
    return not_implemented("cancel recovery")

"""AI investigation endpoints. Owner: reasoner/agent agent.

The reasoner is advisory only (ADR 0004): it proposes hypotheses and
recommended actions; execution is always gated by the deterministic policy
engine."""

from fastapi import APIRouter

from app.api import not_implemented
from app.schemas.agent import InvestigateRequest

router = APIRouter(prefix="/api/v1/incidents", tags=["agent"])


@router.post("/{incident_id}/investigate")
def investigate(incident_id: str, body: InvestigateRequest | None = None):
    # 501 stub: response shape is app.schemas.agent.InvestigateResponse.
    return not_implemented("incident investigation")


@router.get("/{incident_id}/investigation")
def get_investigation(incident_id: str):
    # 501 stub: response shape is app.schemas.agent.InvestigationReportView.
    return not_implemented("investigation report")

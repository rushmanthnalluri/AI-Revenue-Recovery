"""AI investigation endpoints. Owner: reasoner/agent agent.

The reasoner is advisory only (ADR 0004): it proposes hypotheses and
recommended actions; execution is always gated by the deterministic policy
engine. Default mode is the deterministic offline heuristic reasoner; the LLM
reasoner is optional and advisory only.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AgentReport
from app.schemas.agent import (
    AiInferenceView,
    AlternativeHypothesisView,
    HypothesisView,
    InvestigateRequest,
    InvestigateResponse,
    InvestigationReportView,
    ObservedFactView,
    PolicyOutcomePreview,
    RecommendedActionView,
    RevenueImplicationsView,
)
from app.services.agent.report import InvestigationOutput
from app.services.agent.service import AgentError, AgentService, IncidentNotFoundError

router = APIRouter(prefix="/api/v1/incidents", tags=["agent"])


def _action_view(a) -> RecommendedActionView:
    return RecommendedActionView(
        action_type=a.action_type,
        rationale=a.rationale,
        amount_paise=a.amount_paise,
        currency=a.currency,
        payment_id=a.payment_id,
        opportunity_id=a.opportunity_id,
        expected_recovery_paise=a.expected_recovery_paise,
        confidence=a.confidence,
        policy_preview=PolicyOutcomePreview(**a.policy_preview.model_dump())
        if a.policy_preview
        else None,
    )


def _report_view(row: AgentReport) -> InvestigationReportView:
    out = InvestigationOutput.model_validate(row.output or {})
    return InvestigationReportView(
        id=row.id,
        incident_id=row.incident_id or "",
        summary=out.what_happened,
        observed_facts=[ObservedFactView(**f.model_dump()) for f in out.observed_facts],
        ai_inferences=[AiInferenceView(**i.model_dump()) for i in out.ai_inferences],
        recommended_actions=[_action_view(a) for a in out.recommended_actions],
        recommended_next_step=_action_view(out.recommended_next_step)
        if out.recommended_next_step
        else None,
        recommended_candidates=[_action_view(c) for c in out.recommended_candidates],
        alternative_hypotheses=[
            AlternativeHypothesisView(**h.model_dump()) for h in out.alternative_hypotheses
        ],
        revenue_implications=RevenueImplicationsView(**out.revenue_implications.model_dump())
        if out.revenue_implications
        else None,
        uncertainties=list(out.uncertainties),
        confidence=out.confidence,
        escalated=out.escalated,
        escalation_reasons=list(out.escalation_reasons),
        degraded=out.degraded,
        degraded_reasons=list(out.degraded_reasons),
        stripped_claims=list(out.stripped_claims),
        tools_called=list(out.tools_called),
        reasoner=out.reasoner,
        diagnosis=out.diagnosis,
        hypotheses=[
            HypothesisView(
                title=i.statement,
                confidence=i.confidence,
                supporting_evidence=list(i.supporting_fact_ids),
            )
            for i in out.ai_inferences
        ],
        generated_by=row.model,
        tokens_used=row.tokens_used,
        duration_ms=row.duration_ms,
        created_at=row.created_at,
        raw=row.output or {},
    )


@router.post("/{incident_id}/investigate", response_model=InvestigateResponse)
def investigate(
    incident_id: str,
    body: InvestigateRequest | None = None,
    db: Session = Depends(get_db),
) -> InvestigateResponse:
    """Run (or return) an AI investigation for an incident.

    Runs the diagnosis first when none exists, then the reasoner, persists the
    run in ``agent_reports`` and audits it. With ``force_refresh`` a fresh
    report is produced even if one already exists.
    """
    service = AgentService(db)
    try:
        row = service.investigate(
            incident_id, force_refresh=bool(body and body.force_refresh)
        )
    except IncidentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return InvestigateResponse(
        report_id=row.id,
        incident_id=incident_id,
        status=row.status,
        started_at=row.created_at,
        report=_report_view(row),
    )


@router.get("/{incident_id}/investigation", response_model=InvestigationReportView)
def get_investigation(
    incident_id: str,
    db: Session = Depends(get_db),
) -> InvestigationReportView:
    """Latest completed investigation report for an incident."""
    service = AgentService(db)
    row = service.latest(incident_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"no investigation report for incident {incident_id!r}",
        )
    return _report_view(row)

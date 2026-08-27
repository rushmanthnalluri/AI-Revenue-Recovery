"""Recovery endpoints. Owner: recovery execution engineer.

Opportunity-centric contract (the frontend calls /api/v1/recovery/{opp_id}/...):
- GET    /opportunities                list + filters + pagination
- POST   /opportunities/build          incident -> opportunities + strategies
- GET    /{opportunity_id}             detail: actions, policy decisions, audit
- GET    /{opportunity_id}/plan        strategy comparison + recommendation
- POST   /{opportunity_id}/execute     find-or-create action -> policy gate -> fire
- POST   /{opportunity_id}/approve     PENDING_APPROVAL -> APPROVED
- POST   /{opportunity_id}/reject      -> REJECTED
- POST   /{opportunity_id}/escalate    -> ESCALATED (human handoff)
- POST   /{opportunity_id}/cancel      pre-execution -> CANCELLED
- POST   /reconcile                    operator-triggered sweep (ADR 0011): resolve
                                       UNKNOWN actions + reprocess failed webhooks

Every execution flows through the deterministic policy gate before any gateway
call; the gate's decision is persisted and linked on the action.
"""

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_gateway_dependency
from app.db import get_db
from app.logging import request_id_ctx
from app.models import AuditLog, PolicyDecisionRecord, RecoveryOpportunity
from app.ports import PaymentGateway, PolicyOutcome, RecoveryStatus
from app.schemas.recovery import (
    ActionResponse,
    ApproveRequest,
    AuditRef,
    BuildRequest,
    BuildResponse,
    CancelRequest,
    EscalateRequest,
    ExecuteRequest,
    OpportunityDetail,
    OpportunityListResponse,
    OpportunitySummary,
    PolicyDecisionView,
    PolicyPreview,
    ReconcileRequest,
    ReconcileResponse,
    RecoveryPlan,
    RecoveryActionView,
    RejectRequest,
    StrategyOption,
)
from app.services.policy import PolicyEngine
from app.services.recovery import (
    InvalidStateError,
    OpportunityBuilder,
    RecoveryError,
    RecoveryExecutor,
    RecoveryNotFoundError,
    StrategyGenerator,
    run_reconciliation,
)

router = APIRouter(prefix="/api/v1/recovery", tags=["recovery"])


def get_executor(
    db: Session = Depends(get_db),
    gateway: PaymentGateway = Depends(get_gateway_dependency),
) -> RecoveryExecutor:
    return RecoveryExecutor(db, gateway)


# ---------------------------------------------------------------------------
# serialization helpers
# ---------------------------------------------------------------------------


def _projected_status(opp: RecoveryOpportunity) -> RecoveryStatus:
    """Displayed opportunity status: the latest action's status when actions
    exist (actions are the source of truth — webhook reconciliation updates
    them directly), else the stored opportunity status."""
    actions = list(opp.actions or [])
    if not actions:
        return opp.status
    latest = max(actions, key=lambda a: (a.created_at, a.id))
    return latest.status


def _summary(opp: RecoveryOpportunity) -> OpportunitySummary:
    return OpportunitySummary(
        id=opp.id,
        incident_id=opp.incident_id,
        payment_id=opp.payment_id,
        customer_id=opp.customer_id,
        subscription_id=opp.subscription_id,
        opportunity_type=opp.opportunity_type,
        status=_projected_status(opp),
        amount_paise=opp.amount_paise,
        currency=opp.currency,
        expected_recovery_paise=opp.expected_recovery_paise,
        confidence=opp.confidence,
        risk=opp.risk,
        reason=opp.reason,
        created_at=opp.created_at,
        expires_at=opp.expires_at,
    )


def _decision_view(record: PolicyDecisionRecord | None) -> PolicyDecisionView | None:
    if record is None:
        return None
    return PolicyDecisionView(
        id=record.id,
        outcome=record.outcome,
        reasons=list(record.reasons or []),
        rules_matched=list(record.rules_matched or []),
        policy_version=record.policy_version,
        decided_at=record.decided_at,
    )


def _action_view(executor: RecoveryExecutor, action) -> RecoveryActionView:
    return RecoveryActionView(
        id=action.id,
        opportunity_id=action.opportunity_id,
        strategy_id=action.strategy_id,
        action_type=action.action_type,
        status=action.status,
        amount_paise=action.amount_paise,
        currency=action.currency,
        confidence=action.confidence,
        actor=action.actor,
        attempts=action.attempts,
        gateway_request_id=action.gateway_request_id,
        policy_decision=_decision_view(executor.latest_policy_decision(action)),
        proposed_at=action.proposed_at,
        executed_at=action.executed_at,
        verified_at=action.verified_at,
        completed_at=action.completed_at,
        approved_by=action.approved_by,
        note=action.note,
        last_error=action.last_error,
    )


def _action_response(
    executor: RecoveryExecutor, opportunity_id: str, action, message: str
) -> ActionResponse:
    return ActionResponse(
        action_id=action.id if action is not None else None,
        opportunity_id=opportunity_id,
        status=action.status if action is not None else RecoveryStatus.PROPOSED,
        message=message,
        policy_decision=(
            _decision_view(executor.latest_policy_decision(action))
            if action is not None
            else None
        ),
    )


_EXECUTE_MESSAGES = {
    RecoveryStatus.RECOVERED: "executed and verified — revenue recovered",
    RecoveryStatus.VERIFYING: "executed; awaiting webhook/fetch verification",
    RecoveryStatus.FAILED: "gateway definitively rejected the execution (nothing happened)",
    RecoveryStatus.UNKNOWN: "gateway outcome ambiguous; marked UNKNOWN — no blind retry, resolution by re-query",
    RecoveryStatus.PENDING_APPROVAL: "policy requires human approval before execution",
    RecoveryStatus.REJECTED: "blocked by the deterministic policy gate",
}


def _handle_domain_error(exc: RecoveryError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


# ---------------------------------------------------------------------------
# opportunities collection
# ---------------------------------------------------------------------------


@router.get("/opportunities", response_model=OpportunityListResponse)
def list_opportunities(
    db: Session = Depends(get_db),
    status: RecoveryStatus | None = Query(default=None),
    incident_id: str | None = Query(default=None),
    opportunity_type: str | None = Query(default=None),
    customer_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> OpportunityListResponse:
    stmt = sa.select(RecoveryOpportunity)
    count_stmt = sa.select(sa.func.count()).select_from(RecoveryOpportunity)
    filters = []
    if status is not None:
        filters.append(RecoveryOpportunity.status == status)
    if incident_id is not None:
        filters.append(RecoveryOpportunity.incident_id == incident_id)
    if opportunity_type is not None:
        filters.append(RecoveryOpportunity.opportunity_type == opportunity_type)
    if customer_id is not None:
        filters.append(RecoveryOpportunity.customer_id == customer_id)
    if filters:
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)
    total = int(db.execute(count_stmt).scalar_one())
    rows = db.scalars(
        stmt.order_by(RecoveryOpportunity.created_at.desc(), RecoveryOpportunity.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return OpportunityListResponse(
        items=[_summary(opp) for opp in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/opportunities/build", response_model=BuildResponse)
def build_opportunities(
    body: BuildRequest,
    db: Session = Depends(get_db),
) -> BuildResponse:
    """Idempotently turn an incident's failed payments + abandoned checkouts
    into opportunities, and generate each opportunity's strategy set."""
    builder = OpportunityBuilder(db)
    planner = StrategyGenerator(db)
    try:
        result = builder.build_for_incident(body.incident_id, actor=body.actor)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    for opp in result.created:
        planner.generate(opp)
    db.commit()
    return BuildResponse(
        incident_id=body.incident_id,
        created_count=len(result.created),
        existing_count=len(result.existing),
        opportunities=[_summary(opp) for opp in result.all],
    )


@router.post("/reconcile", response_model=ReconcileResponse)
def reconcile(
    body: ReconcileRequest,
    db: Session = Depends(get_db),
    gateway: PaymentGateway = Depends(get_gateway_dependency),
) -> ReconcileResponse:
    """Operator-triggered reconciliation sweep (ADR 0011): UNKNOWN actions are
    re-queried against gateway truth (GETs only) and failed webhook events are
    re-run through the same handler registry as live intake. Idempotent."""
    report = run_reconciliation(db, gateway, actor=body.actor)
    db.commit()
    return ReconcileResponse(**report.__dict__)


# ---------------------------------------------------------------------------
# single opportunity
# ---------------------------------------------------------------------------


@router.get("/{opportunity_id}", response_model=OpportunityDetail)
def get_opportunity(
    opportunity_id: str,
    db: Session = Depends(get_db),
    gateway: PaymentGateway = Depends(get_gateway_dependency),
) -> OpportunityDetail:
    executor = RecoveryExecutor(db, gateway)
    try:
        opp = executor.get_opportunity(opportunity_id)
    except RecoveryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc

    actions = sorted(opp.actions, key=lambda a: (a.created_at, a.id))
    action_ids = [a.id for a in actions]
    audit_filter = sa.or_(
        sa.and_(
            AuditLog.entity_type == "recovery_opportunity",
            AuditLog.entity_id == opp.id,
        ),
        sa.and_(
            AuditLog.entity_type == "recovery_action",
            AuditLog.entity_id.in_(action_ids or ["__none__"]),
        ),
    )
    audit_rows = db.scalars(
        sa.select(AuditLog).where(audit_filter).order_by(AuditLog.created_at, AuditLog.id)
    )
    return OpportunityDetail(
        **_summary(opp).model_dump(),
        constraints=dict(opp.constraints or {}),
        actions=[_action_view(executor, a) for a in actions],
        audit=[
            AuditRef(
                id=row.id,
                actor=row.actor,
                action=row.action,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                request_id=row.request_id,
                details=dict(row.details or {}),
                created_at=row.created_at,
            )
            for row in audit_rows
        ],
    )


@router.get("/{opportunity_id}/plan", response_model=RecoveryPlan)
def get_plan(
    opportunity_id: str,
    db: Session = Depends(get_db),
    gateway: PaymentGateway = Depends(get_gateway_dependency),
) -> RecoveryPlan:
    executor = RecoveryExecutor(db, gateway)
    try:
        opp = executor.get_opportunity(opportunity_id)
    except RecoveryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc

    planner = StrategyGenerator(db)
    strategies = planner.generate(opp)  # find-or-create; first call persists
    recommended = next((s for s in strategies if s.selected), None)

    # Policy preview for the recommended strategy: a REAL evaluation through
    # the gate (persisted as a policy_decisions row with actor
    # system:plan_preview and no action link) so the table shows what the
    # deterministic gate would say right now.
    preview: PolicyPreview | None = None
    if recommended is not None:
        from app.ports import ActionContext

        decision = PolicyEngine.from_file(session=db).evaluate(
            ActionContext(
                action_type=recommended.action_type,
                amount_paise=opp.amount_paise,
                confidence=recommended.confidence,
                actor="system:plan_preview",
                currency=opp.currency or "INR",
                incident_id=opp.incident_id,
                opportunity_id=opp.id,
                customer_id=opp.customer_id,
            )
        )
        preview = PolicyPreview(outcome=decision.outcome, reasons=list(decision.reasons))

    return RecoveryPlan(
        opportunity_id=opp.id,
        strategies=[
            StrategyOption(
                id=s.id,
                action_type=s.action_type,
                rank=s.rank,
                expected_recovery_paise=s.expected_recovery_paise,
                confidence=s.confidence,
                risk=s.risk,
                eligibility=s.eligibility,
                reason=s.reason,
                constraints=dict(s.constraints or {}),
                generated_by=s.generated_by,
                selected=s.selected,
            )
            for s in strategies
        ],
        recommended_strategy_id=recommended.id if recommended else None,
        policy_preview=preview,
    )


# ---------------------------------------------------------------------------
# mutations (all policy-gated; actor comes from the request body)
# ---------------------------------------------------------------------------


@router.post("/{opportunity_id}/execute", response_model=ActionResponse)
def execute(
    opportunity_id: str,
    body: ExecuteRequest,
    executor: RecoveryExecutor = Depends(get_executor),
    db: Session = Depends(get_db),
) -> ActionResponse:
    try:
        action = executor.execute(
            opportunity_id,
            strategy_id=body.strategy_id,
            actor=body.actor,
            request_id=request_id_ctx.get(),
        )
    except RecoveryError as exc:
        db.rollback()
        _handle_domain_error(exc)
    db.commit()
    message = _EXECUTE_MESSAGES.get(action.status, action.status.value)
    if action.status is RecoveryStatus.UNKNOWN and action.last_error:
        message = f"{message} ({action.last_error})"
    return _action_response(executor, opportunity_id, action, message)


@router.post("/{opportunity_id}/approve", response_model=ActionResponse)
def approve(
    opportunity_id: str,
    body: ApproveRequest,
    executor: RecoveryExecutor = Depends(get_executor),
    db: Session = Depends(get_db),
) -> ActionResponse:
    try:
        action = executor.approve(
            opportunity_id,
            actor=body.actor,
            note=body.note,
            request_id=request_id_ctx.get(),
        )
    except RecoveryError as exc:
        db.rollback()
        _handle_domain_error(exc)
    db.commit()
    return _action_response(
        executor, opportunity_id, action, "approved by human; ready to execute"
    )


@router.post("/{opportunity_id}/reject", response_model=ActionResponse)
def reject(
    opportunity_id: str,
    body: RejectRequest,
    executor: RecoveryExecutor = Depends(get_executor),
    db: Session = Depends(get_db),
) -> ActionResponse:
    try:
        action = executor.reject(
            opportunity_id,
            actor=body.actor,
            reason=body.reason,
            request_id=request_id_ctx.get(),
        )
    except RecoveryError as exc:
        db.rollback()
        _handle_domain_error(exc)
    db.commit()
    if action is None:
        opp = executor.get_opportunity(opportunity_id)
        return ActionResponse(
            action_id=None,
            opportunity_id=opportunity_id,
            status=opp.status,
            message=f"opportunity rejected: {body.reason}",
        )
    return _action_response(executor, opportunity_id, action, f"rejected: {body.reason}")


@router.post("/{opportunity_id}/escalate", response_model=ActionResponse)
def escalate(
    opportunity_id: str,
    body: EscalateRequest,
    executor: RecoveryExecutor = Depends(get_executor),
    db: Session = Depends(get_db),
) -> ActionResponse:
    try:
        action = executor.escalate(
            opportunity_id,
            actor=body.actor,
            reason=body.reason,
            request_id=request_id_ctx.get(),
        )
    except RecoveryError as exc:
        db.rollback()
        _handle_domain_error(exc)
    db.commit()
    if action is None:
        opp = executor.get_opportunity(opportunity_id)
        return ActionResponse(
            action_id=None,
            opportunity_id=opportunity_id,
            status=opp.status,
            message=f"opportunity escalated to a human: {body.reason}",
        )
    return _action_response(
        executor, opportunity_id, action, f"escalated to a human: {body.reason}"
    )


@router.post("/{opportunity_id}/cancel", response_model=ActionResponse)
def cancel(
    opportunity_id: str,
    body: CancelRequest,
    executor: RecoveryExecutor = Depends(get_executor),
    db: Session = Depends(get_db),
) -> ActionResponse:
    try:
        action = executor.cancel(
            opportunity_id,
            actor=body.actor,
            reason=body.reason,
            request_id=request_id_ctx.get(),
        )
    except RecoveryError as exc:
        db.rollback()
        _handle_domain_error(exc)
    db.commit()
    if action is None:
        opp = executor.get_opportunity(opportunity_id)
        return ActionResponse(
            action_id=None,
            opportunity_id=opportunity_id,
            status=opp.status,
            message="opportunity cancelled",
        )
    return _action_response(executor, opportunity_id, action, "cancelled")


__all__ = ["router", "get_gateway_dependency", "get_executor"]

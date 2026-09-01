"""Recovery endpoints. Owner: recovery execution engineer.

Opportunity-centric contract (the frontend calls /api/v1/recovery/{opp_id}/...):
- GET    /opportunities                list + filters + pagination
- GET    /opportunities/approvals-summary  whole-queue COUNT/SUM for the
                                           pending-approval lane (page-independent)
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

Every mutation also binds a KYA-lite principal (X-API-Key-derived, see
app.api.deps.get_principal): the executor receives the principal-attributed
actor string, one additive `recovery.principal_bound` audit row records the
binding, and approve re-gates the action with proposer/approver principals so
a self-/same-cohort approval leaves a `separation_of_duties.self_approval`
warning on a persisted policy decision. Demo-grade identity, not SSO —
docs/security-testing.md.
"""

import sqlalchemy as sa
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, selectinload

from app.api.deps import Principal, get_gateway_dependency, get_principal
from app.db import get_db
from app.logging import request_id_ctx
from app.models import AuditLog, PolicyDecisionRecord, RecoveryAction, RecoveryOpportunity
from app.ports import ActionContext, PaymentGateway, PolicyOutcome, RecoveryStatus
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
from app.services.policy import PolicyEngine, audit
from app.services.policy.engine import (
    META_APPROVER_PRINCIPAL,
    META_CURRENT_ACTION_ID,
    META_PROPOSER_PRINCIPAL,
    META_REQUEST_ID,
)
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
        environment=opp.environment or "research",
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


def _latest_decisions_for_actions(
    db: Session, actions: list[RecoveryAction]
) -> dict[str, PolicyDecisionRecord | None]:
    """Batch version of `RecoveryExecutor.latest_policy_decision` for the
    detail view — two IN-queries total (records linked on the actions, then
    records keyed by action_id) instead of 1-2 queries PER action. Selection
    semantics match the per-action version exactly: the record linked via
    `policy_decision_id` wins; otherwise the newest by (created_at, id)."""
    linked: dict[str, PolicyDecisionRecord] = {}
    linked_ids = [a.policy_decision_id for a in actions if a.policy_decision_id]
    if linked_ids:
        for rec in db.scalars(
            sa.select(PolicyDecisionRecord).where(PolicyDecisionRecord.id.in_(linked_ids))
        ):
            linked[rec.id] = rec
    latest: dict[str, PolicyDecisionRecord] = {}
    action_ids = [a.id for a in actions]
    if action_ids:
        rows = db.scalars(
            sa.select(PolicyDecisionRecord).where(
                PolicyDecisionRecord.action_id.in_(action_ids)
            )
        )
        for rec in rows:
            current = latest.get(rec.action_id)
            if current is None or (rec.created_at, rec.id) > (current.created_at, current.id):
                latest[rec.action_id] = rec
    result: dict[str, PolicyDecisionRecord | None] = {}
    for action in actions:
        rec = linked.get(action.policy_decision_id) if action.policy_decision_id else None
        result[action.id] = rec if rec is not None else latest.get(action.id)
    return result


_UNSET = object()


def _action_view(
    executor: RecoveryExecutor, action, decision: PolicyDecisionRecord | None | object = _UNSET
) -> RecoveryActionView:
    # `decision` lets the detail endpoint pass a batch-prefetched record;
    # single-action callers (the mutation responses) keep the per-action
    # lookup, which is one query there.
    if decision is _UNSET:
        decision = executor.latest_policy_decision(action)
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
        policy_decision=_decision_view(decision),
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
# KYA-lite principal binding (demo-grade — docs/security-testing.md)
# ---------------------------------------------------------------------------


def _record_principal_binding(
    db: Session,
    *,
    principal: Principal,
    declared_actor: str,
    endpoint: str,
    action: RecoveryAction | None,
    opportunity_id: str,
    environment: str | None,
    request_id: str | None,
) -> None:
    """Persist WHICH authenticated principal performed a mutating recovery
    call: one additive row in the same append-only audit trail (same shape —
    the executor's own transition rows are untouched). KYA-lite, not SSO: the
    principal comes from the shared API key, so it binds a cohort, and the
    self-declared actor is recorded alongside it rather than replaced.

    The row keys to the OPPORTUNITY (the action id rides in details): every
    verb binds uniformly — including opportunity-level reject/escalate/cancel
    where no action exists — and per-action transition trails keep their
    exact recovery.action.* shape."""
    entry = audit.record(
        db,
        actor=principal.attributed_actor(declared_actor),
        action="recovery.principal_bound",
        entity_type="recovery_opportunity",
        entity_id=opportunity_id,
        details={
            "principal_id": principal.id,
            "declared_actor": declared_actor,
            "endpoint": endpoint,
            "authenticated": principal.authenticated,
            "action_id": action.id if action is not None else None,
        },
        request_id=request_id,
    )
    entry.environment = environment or "research"


def _separation_of_duties_regate(
    db: Session,
    *,
    action: RecoveryAction,
    approver: Principal,
    declared_actor: str,
    request_id: str | None,
) -> None:
    """Re-run the deterministic gate at approval time carrying the proposer
    and approver principals; a self-/same-cohort approval records the
    `separation_of_duties.self_approval` warning on the persisted decision
    (never blocks — the signal is the point; see engine R10).

    The record IS linked to the action (so the warning is discoverable in the
    action's decision history) but does NOT relink `action.policy_decision_id`
    — the gate decision that parked the action stays the displayed one. This
    is a recorded signal, not a re-authorization: the human approval has
    already been stamped by the executor.

    An unattributed proposer (the norm under the shared demo key) resolves to
    the approver's own principal — the conservative worst case, so the check
    fails toward a warning, not toward silence."""
    proposer_principal = Principal.principal_of_actor(action.actor) or approver.id
    opp = action.opportunity
    PolicyEngine.from_file(session=db).evaluate(
        ActionContext(
            action_type=action.action_type,
            amount_paise=action.amount_paise,
            confidence=action.confidence,
            actor=approver.attributed_actor(declared_actor),
            currency=action.currency or "INR",
            incident_id=action.incident_id,
            opportunity_id=action.opportunity_id,
            customer_id=opp.customer_id if opp is not None else None,
            metadata={
                META_CURRENT_ACTION_ID: action.id,  # exclude self from history guards
                META_REQUEST_ID: request_id or "",
                META_PROPOSER_PRINCIPAL: proposer_principal,
                META_APPROVER_PRINCIPAL: approver.id,
            },
        )
    )


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
    environment: Literal["real_test", "research"] = Query(default="real_test"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> OpportunityListResponse:
    stmt = sa.select(RecoveryOpportunity).options(
        # The displayed status is the latest action's status (_projected_status),
        # so every row needs its actions — eager-load them in ONE extra query
        # instead of one lazy load per row (up to 200 at max page size).
        selectinload(RecoveryOpportunity.actions)
    )
    count_stmt = sa.select(sa.func.count()).select_from(RecoveryOpportunity)
    filters = [RecoveryOpportunity.environment == environment]
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


class ApprovalsSummaryResponse(BaseModel):
    """Whole-queue aggregate for the pending-approvals lane.

    Defined here (not app/schemas/recovery.py) deliberately: it is owned with
    this router; migrate it into the schemas module if it is reused
    elsewhere. Additive — no existing response shape changes."""

    environment: str
    status: str
    pending_count: int
    pending_amount_paise: int


@router.get("/opportunities/approvals-summary", response_model=ApprovalsSummaryResponse)
def approvals_summary(
    db: Session = Depends(get_db),
    environment: Literal["real_test", "research"] = Query(default="real_test"),
) -> ApprovalsSummaryResponse:
    """SQL-side COUNT/SUM over the ENTIRE pending-approval queue for one
    environment. The Approval Center's 'Value awaiting decision' metric sums
    page 1 of the opportunities list client-side; this aggregate is the
    correct value beyond page 1. Mirrors the list endpoint's queue definition
    (stored opportunity status + environment stamp)."""
    count, total = db.execute(
        sa.select(
            sa.func.count(),
            sa.func.coalesce(sa.func.sum(RecoveryOpportunity.amount_paise), 0),
        )
        .where(RecoveryOpportunity.environment == environment)
        .where(RecoveryOpportunity.status == RecoveryStatus.PENDING_APPROVAL)
    ).one()
    return ApprovalsSummaryResponse(
        environment=environment,
        status=RecoveryStatus.PENDING_APPROVAL.value,
        pending_count=int(count),
        pending_amount_paise=int(total),
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
    decisions = _latest_decisions_for_actions(db, actions)
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
        actions=[_action_view(executor, a, decision=decisions[a.id]) for a in actions],
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

    db.commit()  # persists generated strategies + the plan-preview policy decision
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
    principal: Principal = Depends(get_principal),
) -> ActionResponse:
    rid = request_id_ctx.get()
    try:
        action = executor.execute(
            opportunity_id,
            strategy_id=body.strategy_id,
            actor=principal.attributed_actor(body.actor),
            request_id=rid,
        )
    except RecoveryError as exc:
        db.rollback()
        _handle_domain_error(exc)
    _record_principal_binding(
        db,
        principal=principal,
        declared_actor=body.actor,
        endpoint="execute",
        action=action,
        opportunity_id=opportunity_id,
        environment=action.environment,
        request_id=rid,
    )
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
    principal: Principal = Depends(get_principal),
) -> ActionResponse:
    rid = request_id_ctx.get()
    try:
        action = executor.approve(
            opportunity_id,
            actor=principal.attributed_actor(body.actor),
            note=body.note,
            request_id=rid,
        )
    except RecoveryError as exc:
        db.rollback()
        _handle_domain_error(exc)
    # KYA-lite: record who approved (key-derived principal) and let the
    # policy gate flag a self-/same-cohort approval. Both are additive
    # signals; neither changes the approval the executor just stamped.
    _separation_of_duties_regate(
        db,
        action=action,
        approver=principal,
        declared_actor=body.actor,
        request_id=rid,
    )
    _record_principal_binding(
        db,
        principal=principal,
        declared_actor=body.actor,
        endpoint="approve",
        action=action,
        opportunity_id=opportunity_id,
        environment=action.environment,
        request_id=rid,
    )
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
    principal: Principal = Depends(get_principal),
) -> ActionResponse:
    rid = request_id_ctx.get()
    try:
        action = executor.reject(
            opportunity_id,
            actor=principal.attributed_actor(body.actor),
            reason=body.reason,
            request_id=rid,
        )
    except RecoveryError as exc:
        db.rollback()
        _handle_domain_error(exc)
    if action is None:
        opp = executor.get_opportunity(opportunity_id)
        environment = opp.environment
    else:
        environment = action.environment
    _record_principal_binding(
        db,
        principal=principal,
        declared_actor=body.actor,
        endpoint="reject",
        action=action,
        opportunity_id=opportunity_id,
        environment=environment,
        request_id=rid,
    )
    db.commit()
    if action is None:
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
    principal: Principal = Depends(get_principal),
) -> ActionResponse:
    rid = request_id_ctx.get()
    try:
        action = executor.escalate(
            opportunity_id,
            actor=principal.attributed_actor(body.actor),
            reason=body.reason,
            request_id=rid,
        )
    except RecoveryError as exc:
        db.rollback()
        _handle_domain_error(exc)
    if action is None:
        opp = executor.get_opportunity(opportunity_id)
        environment = opp.environment
    else:
        environment = action.environment
    _record_principal_binding(
        db,
        principal=principal,
        declared_actor=body.actor,
        endpoint="escalate",
        action=action,
        opportunity_id=opportunity_id,
        environment=environment,
        request_id=rid,
    )
    db.commit()
    if action is None:
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
    principal: Principal = Depends(get_principal),
) -> ActionResponse:
    rid = request_id_ctx.get()
    try:
        action = executor.cancel(
            opportunity_id,
            actor=principal.attributed_actor(body.actor),
            reason=body.reason,
            request_id=rid,
        )
    except RecoveryError as exc:
        db.rollback()
        _handle_domain_error(exc)
    if action is None:
        opp = executor.get_opportunity(opportunity_id)
        environment = opp.environment
    else:
        environment = action.environment
    _record_principal_binding(
        db,
        principal=principal,
        declared_actor=body.actor,
        endpoint="cancel",
        action=action,
        opportunity_id=opportunity_id,
        environment=environment,
        request_id=rid,
    )
    db.commit()
    if action is None:
        return ActionResponse(
            action_id=None,
            opportunity_id=opportunity_id,
            status=opp.status,
            message="opportunity cancelled",
        )
    return _action_response(executor, opportunity_id, action, "cancelled")


__all__ = ["router", "get_gateway_dependency", "get_executor"]

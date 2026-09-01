"""Recovery executor: the closed-loop state machine for recovery_actions.

    PROPOSED -> POLICY_EVALUATED -> ALLOWED          -> auto-execute
                                 -> REQUIRES_APPROVAL -> PENDING_APPROVAL
                                 -> BLOCKED           -> REJECTED (terminal)
    PENDING_APPROVAL --approve--> APPROVED -> execute
    PENDING_APPROVAL --reject-->  REJECTED (terminal)
    PENDING_APPROVAL --approval TTL exceeded--> PROPOSED (lapse-on-read;
        only when the policy sets `approval.pending_approval_ttl_hours`)
    any pre-execution state --cancel--> CANCELLED (terminal)
    any non-terminal state --escalate--> ESCALATED (terminal)
    ALLOWED delayed retry (constraints.delay_seconds) --> SCHEDULED: parked
        until due, then fired through the normal execute() path by the
        in-process worker (docs/worker.md) — same re-gate, same guards.
    EXECUTING -> VERIFYING -> RECOVERED | FAILED
    EXECUTING -> UNKNOWN   (gateway gave no authoritative answer)

Safety invariants (enforced here, proven by tests/recovery):
- EVERY execution passes PolicyEngine.evaluate first — AI output is advisory.
- One gateway mutation per action, ever: `gateway_request_id` is the
  idempotency key (mapped to Razorpay `receipt` / `reference_id`), and a
  second execute on the same opportunity reuses the open action instead of
  creating a new one. Cross-opportunity duplicates are BLOCKED by the policy
  gate's duplicate-protection guard. A SCHEDULED action still holds the
  opportunity's execution slot; firing reuses the same action and key.
- GatewayTransientError (timeout / 5xx / unreadable response) -> UNKNOWN.
  NEVER blind-retry a mutating call; resolve by re-querying gateway truth
  (fetch_payment / fetch_order) via `resolve()`.
- GatewayClientError (4xx) -> FAILED: the gateway rejected the request before
  processing it, so nothing happened — a definitive, truthful failure.
- Every state transition appends an audit_logs row with actor + request_id.

Transaction boundary: this service flushes but NEVER commits (same convention
as the policy engine and audit helper); the API layer commits.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import ids
from app.config import settings
from app.db import utcnow
from app.logging import get_logger, request_id_ctx
from app.models import (
    Customer,
    NotificationOutbox,
    Payment,
    PolicyDecisionRecord,
    RecoveryAction,
    RecoveryOpportunity,
    RecoveryStrategy,
)
from app.models.base import ENVIRONMENT_REAL_TEST, ENVIRONMENT_RESEARCH
from app.ports import (
    ActionContext,
    ActionType,
    NotificationStatus,
    PaymentGateway,
    PolicyDecision,
    PolicyOutcome,
    RecoveryStatus,
)
from app.services.policy import PolicyEngine, audit
from app.services.policy.engine import (
    META_CURRENT_ACTION_ID,
    META_REQUEST_ID,
    META_STRATEGY_ID,
)
from app.services.policy.history import SqlPolicyHistory
from app.services.razorpay import factory as gateway_factory
from app.services.razorpay.errors import (
    GatewayClientError,
    GatewayNotFoundError,
    GatewayTransientError,
)
from app.services.recovery.strategies import StrategyGenerator

logger = get_logger(__name__)

# States in which an action still occupies the opportunity's execution slot.
OPEN_STATES = (
    RecoveryStatus.PROPOSED,
    RecoveryStatus.POLICY_EVALUATED,
    RecoveryStatus.PENDING_APPROVAL,
    RecoveryStatus.APPROVED,
    RecoveryStatus.SCHEDULED,
    RecoveryStatus.EXECUTING,
    RecoveryStatus.VERIFYING,
    RecoveryStatus.UNKNOWN,
)
# A gateway call may be in flight or awaiting verification — refuse to disturb.
IN_FLIGHT_STATES = (RecoveryStatus.EXECUTING, RecoveryStatus.VERIFYING)
# Pre-execution states a human may still cancel (nothing reached the gateway).
CANCELLABLE_STATES = (
    RecoveryStatus.PROPOSED,
    RecoveryStatus.POLICY_EVALUATED,
    RecoveryStatus.PENDING_APPROVAL,
    RecoveryStatus.APPROVED,
    RecoveryStatus.SCHEDULED,
)
TERMINAL_STATES = (
    RecoveryStatus.RECOVERED,
    RecoveryStatus.FAILED,
    RecoveryStatus.REJECTED,
    RecoveryStatus.CANCELLED,
    RecoveryStatus.ESCALATED,
)

# Actor stamped on approval-TTL lapse transitions and their policy decision
# records (policy rule `approval.pending_approval_ttl_hours`).
APPROVAL_TTL_ACTOR = "system:approval_ttl"
# The policy rule name recorded on lapse decisions; also the (optional)
# configuration key that enables the lapse.
APPROVAL_TTL_RULE = "approval.pending_approval_ttl_hours"


class RecoveryError(Exception):
    """Domain error carrying an HTTP-ish status code for the API layer."""

    status_code = 409
    code = "recovery_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RecoveryNotFoundError(RecoveryError):
    status_code = 404
    code = "not_found"


class InvalidStateError(RecoveryError):
    status_code = 409
    code = "invalid_state"


class GatewayNotConfiguredError(RecoveryError):
    """A real_test execution was requested but no real Razorpay keys are
    configured. The executor refuses: NEVER a fake execution, NEVER the
    simulator for a real_test opportunity."""

    status_code = 409
    code = "razorpay_not_configured"


class RecoveryExecutor:
    """Drives recovery actions through policy, gateway, and verification.

    Gateway-by-environment: ``research`` opportunities execute against the
    injected gateway (the simulation twin in every current deployment);
    ``real_test`` opportunities execute against the REAL Razorpay adapter —
    ``real_gateway`` when injected (test seam), else the configured adapter,
    else an honest :class:`GatewayNotConfiguredError` refusal.
    """

    def __init__(
        self,
        session: Session,
        gateway: PaymentGateway,
        *,
        policy_engine: PolicyEngine | None = None,
        real_gateway: PaymentGateway | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = session
        self._gw = gateway
        self._real_gateway = real_gateway
        self._policy = policy_engine or PolicyEngine.from_file(session=session)
        self._history = SqlPolicyHistory(session)
        self._strategies = StrategyGenerator(session)
        # Timestamp source; the worker injects its clock so delayed-retry
        # due-evaluation is deterministic under test. Defaults to utcnow.
        self._clock = clock or utcnow

    def _gateway_for(self, opp: RecoveryOpportunity | None) -> PaymentGateway:
        """Route the gateway call by the opportunity's environment stamp."""
        environment = (opp.environment if opp is not None else None) or ENVIRONMENT_RESEARCH
        if environment != ENVIRONMENT_REAL_TEST:
            return self._gw
        if self._real_gateway is not None:
            return self._real_gateway
        real = gateway_factory.get_real_gateway(settings)
        if real is None:
            raise GatewayNotConfiguredError(
                f"razorpay_not_configured: opportunity {opp.id} is environment "
                "'real_test' but RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET are not "
                "configured (or SIMULATION_MODE is on) — refusing to execute; "
                "a real_test action never touches the simulator"
            )
        return real

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get_opportunity(self, opportunity_id: str) -> RecoveryOpportunity:
        opp = self._db.get(RecoveryOpportunity, opportunity_id)
        if opp is None:
            raise RecoveryNotFoundError(f"opportunity not found: {opportunity_id!r}")
        return opp

    def _lock_opportunity(self, opportunity_id: str) -> RecoveryOpportunity:
        """SELECT ... FOR UPDATE on the opportunity row (Postgres: serializes
        concurrent executors — a racing duplicate execute waits for the
        winner's commit, then sees the winner's action and is refused/blocked
        instead of double-firing the gateway. Silently omitted on SQLite,
        where writer serialization already orders the race)."""
        opp = self._db.scalar(
            sa.select(RecoveryOpportunity)
            .where(RecoveryOpportunity.id == opportunity_id)
            .with_for_update()
        )
        if opp is None:
            raise RecoveryNotFoundError(f"opportunity not found: {opportunity_id!r}")
        return opp

    def open_action_for(self, opportunity_id: str) -> RecoveryAction | None:
        """The action currently occupying the opportunity's execution slot."""
        stmt = (
            sa.select(RecoveryAction)
            .where(
                RecoveryAction.opportunity_id == opportunity_id,
                RecoveryAction.status.in_(OPEN_STATES),
            )
            .order_by(RecoveryAction.created_at.desc(), RecoveryAction.id.desc())
            .limit(1)
        )
        action = self._db.scalar(stmt)
        if action is not None:
            # Lapse-on-read: a PENDING_APPROVAL action older than the
            # configured approval TTL returns to PROPOSED here, releasing the
            # approval wait before any caller acts on the stale state.
            self._lapse_stale_approval(action)
        return action

    def _approval_ttl(self) -> timedelta | None:
        """The configured approval TTL, or None when the policy document does
        not set `approval.pending_approval_ttl_hours` (the shipped default —
        lapse disabled, approvals wait indefinitely)."""
        hours = self._policy.config.approval.pending_approval_ttl_hours
        if hours is None:
            return None
        return timedelta(hours=hours)

    def _lapse_stale_approval(self, action: RecoveryAction) -> None:
        """Lapse a PENDING_APPROVAL action whose wait EXCEEDS the configured
        TTL back to PROPOSED, with a policy decision record and the usual
        transition audit row (actor system:approval_ttl). At exactly the TTL
        the approval is still live — the wait must exceed it."""
        ttl = self._approval_ttl()
        if ttl is None or action.status is not RecoveryStatus.PENDING_APPROVAL:
            return
        # The approval wait started when the gate parked the action
        # (decided_at); proposed_at covers rows that predate that stamp.
        since = action.decided_at or action.proposed_at
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        now = self._clock()
        age = now - since
        if age <= ttl:
            return
        record = PolicyDecisionRecord(
            action_id=action.id,
            action_type=action.action_type.value,
            amount_paise=action.amount_paise,
            currency=action.currency or "INR",
            confidence=action.confidence,
            outcome=PolicyOutcome.REQUIRES_APPROVAL,
            reasons=[
                f"approval wait of {age} exceeded the configured TTL of "
                f"{ttl}; the action lapses back to PROPOSED and must be "
                "re-gated (and re-approved) before it can execute"
            ],
            rules_matched=[APPROVAL_TTL_RULE],
            policy_version=self._policy.policy_version,
            actor=APPROVAL_TTL_ACTOR,
            context={
                "trigger": "approval_ttl_lapse",
                "pending_since": since.isoformat(),
                "age_hours": round(age.total_seconds() / 3600, 4),
                "ttl_hours": round(ttl.total_seconds() / 3600, 4),
            },
            decided_at=now,
        )
        self._db.add(record)
        self._db.flush()
        self._transition(
            action,
            RecoveryStatus.PROPOSED,
            actor=APPROVAL_TTL_ACTOR,
            request_id=None,
            note="approval wait exceeded the configured TTL; lapsed back to PROPOSED",
            details={
                "policy_outcome": PolicyOutcome.REQUIRES_APPROVAL.value,
                "policy_decision_id": record.id,
                "rules_matched": [APPROVAL_TTL_RULE],
                "policy_version": record.policy_version,
                "pending_since": since.isoformat(),
            },
        )

    def latest_policy_decision(
        self, action: RecoveryAction
    ) -> PolicyDecisionRecord | None:
        if action.policy_decision_id:
            record = self._db.get(PolicyDecisionRecord, action.policy_decision_id)
            if record is not None:
                return record
        stmt = (
            sa.select(PolicyDecisionRecord)
            .where(PolicyDecisionRecord.action_id == action.id)
            .order_by(PolicyDecisionRecord.created_at.desc(), PolicyDecisionRecord.id.desc())
            .limit(1)
        )
        return self._db.scalar(stmt)

    # ------------------------------------------------------------------
    # execute (find-or-create the action, gate it, maybe fire)
    # ------------------------------------------------------------------

    def execute(
        self,
        opportunity_id: str,
        *,
        strategy_id: str | None = None,
        actor: str,
        request_id: str | None = None,
    ) -> RecoveryAction:
        """Idempotent execution entry point.

        - No open action: create one from the chosen/recommended strategy and
          run it through the policy gate; ALLOWED fires immediately (a
          delayed-retry strategy parks in SCHEDULED instead — see `_fire`).
        - Open action PENDING_APPROVAL: refuse — a human must approve first.
        - Open action SCHEDULED: not yet due -> no-op (still parked); due ->
          re-gate and fire (the worker calls exactly this path).
        - Open action EXECUTING/VERIFYING: refuse — a gateway call is live.
        - Open action UNKNOWN: NO blind retry — re-query gateway truth instead.
        """
        rid = self._rid(request_id)
        opp = self._lock_opportunity(opportunity_id)
        action = self.open_action_for(opp.id)

        if action is not None and action.status in IN_FLIGHT_STATES:
            raise InvalidStateError(
                f"action {action.id} is {action.status.value}; a gateway call is "
                "already in flight or awaiting verification — duplicate execution refused"
            )
        if action is not None and action.status is RecoveryStatus.UNKNOWN:
            # Timeout/ambiguous outcome: never re-fire the mutation. Re-query.
            return self.resolve(action.id, actor=actor, request_id=rid)
        if action is not None and action.status is RecoveryStatus.PENDING_APPROVAL:
            raise InvalidStateError(
                f"action {action.id} awaits human approval; execute is refused "
                "until approve (or reject) lands"
            )

        if action is None:
            strategy = self._pick_strategy(opp, strategy_id)
            action = self._create_action(opp, strategy, actor=actor, request_id=rid)
        elif strategy_id and action.strategy_id != strategy_id:
            raise InvalidStateError(
                f"open action {action.id} already follows strategy "
                f"{action.strategy_id}; refusing to switch to {strategy_id}"
            )

        if action.status is RecoveryStatus.SCHEDULED and not self.scheduled_due(action):
            # Parked delayed retry, still waiting: idempotent no-op. The due
            # case falls through and is re-gated below before firing — the
            # deterministic gate decides again at fire time (duplicate
            # protection, stopping rules and rate limits re-checked fresh).
            return action

        # A parked action reaches the gate only when due (the guard above
        # returned otherwise); remember the parking across the re-gate, which
        # re-stamps status and decided_at, so `_fire` does not re-park it.
        firing_from_scheduled = action.status is RecoveryStatus.SCHEDULED

        if action.status is not RecoveryStatus.APPROVED:
            decision = self._gate(action, actor=actor, request_id=rid)
            if decision.outcome is PolicyOutcome.BLOCKED:
                self._transition(
                    action,
                    RecoveryStatus.REJECTED,
                    actor=actor,
                    request_id=rid,
                    note="blocked by the deterministic policy gate",
                    details={
                        "policy_outcome": decision.outcome.value,
                        "rules_matched": decision.rules_matched,
                    },
                )
                return action
            if decision.outcome is PolicyOutcome.REQUIRES_APPROVAL:
                self._transition(
                    action,
                    RecoveryStatus.PENDING_APPROVAL,
                    actor=actor,
                    request_id=rid,
                    note="policy requires human approval before execution",
                    details={
                        "policy_outcome": decision.outcome.value,
                        "rules_matched": decision.rules_matched,
                    },
                )
                return action
        # ALLOWED by policy, or APPROVED by a human after REQUIRES_APPROVAL.
        self._fire(action, actor=actor, request_id=rid, from_scheduled=firing_from_scheduled)
        return action

    # ------------------------------------------------------------------
    # human decisions
    # ------------------------------------------------------------------

    def approve(
        self,
        opportunity_id: str,
        *,
        actor: str,
        note: str | None = None,
        request_id: str | None = None,
    ) -> RecoveryAction:
        rid = self._rid(request_id)
        opp = self._lock_opportunity(opportunity_id)
        action = self.open_action_for(opp.id)
        if action is None or action.status is not RecoveryStatus.PENDING_APPROVAL:
            raise InvalidStateError(
                f"opportunity {opp.id} has no action awaiting approval"
            )
        action.approved_at = self._clock()
        action.approved_by = actor
        self._transition(
            action,
            RecoveryStatus.APPROVED,
            actor=actor,
            request_id=rid,
            note=note,
            details={"policy_outcome": PolicyOutcome.REQUIRES_APPROVAL.value},
        )
        return action

    def reject(
        self,
        opportunity_id: str,
        *,
        actor: str,
        reason: str,
        request_id: str | None = None,
    ) -> RecoveryAction | None:
        rid = self._rid(request_id)
        opp = self._lock_opportunity(opportunity_id)
        action = self.open_action_for(opp.id)
        if action is None:
            self._opportunity_level(
                opp, RecoveryStatus.REJECTED, actor=actor, request_id=rid, note=reason
            )
            return None
        if action.status in IN_FLIGHT_STATES or action.status is RecoveryStatus.UNKNOWN:
            raise InvalidStateError(
                f"action {action.id} is {action.status.value}; it already reached "
                "the gateway (or its outcome is ambiguous) and cannot be rejected"
            )
        self._transition(
            action, RecoveryStatus.REJECTED, actor=actor, request_id=rid, note=reason
        )
        return action

    def escalate(
        self,
        opportunity_id: str,
        *,
        actor: str,
        reason: str,
        request_id: str | None = None,
    ) -> RecoveryAction | None:
        """Hand off to a human operator. Allowed from ANY non-terminal state —
        including UNKNOWN (a human investigates the ambiguous outcome) — and
        terminal for automation: webhooks/pollers no longer move the action."""
        rid = self._rid(request_id)
        opp = self._lock_opportunity(opportunity_id)
        action = self.open_action_for(opp.id)
        if action is None:
            self._opportunity_level(
                opp, RecoveryStatus.ESCALATED, actor=actor, request_id=rid, note=reason
            )
            return None
        self._transition(
            action,
            RecoveryStatus.ESCALATED,
            actor=actor,
            request_id=rid,
            note=reason,
            details={"handoff": "human_ops"},
        )
        return action

    def cancel(
        self,
        opportunity_id: str,
        *,
        actor: str,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> RecoveryAction | None:
        rid = self._rid(request_id)
        opp = self._lock_opportunity(opportunity_id)
        action = self.open_action_for(opp.id)
        if action is None:
            self._opportunity_level(
                opp, RecoveryStatus.CANCELLED, actor=actor, request_id=rid, note=reason
            )
            return None
        if action.status not in CANCELLABLE_STATES:
            raise InvalidStateError(
                f"action {action.id} is {action.status.value}; only pre-execution "
                "actions can be cancelled (a fired or ambiguous action must be "
                "resolved, not retracted)"
            )
        self._transition(
            action, RecoveryStatus.CANCELLED, actor=actor, request_id=rid, note=reason
        )
        return action

    # ------------------------------------------------------------------
    # UNKNOWN resolution — truth by re-query, never by blind retry
    # ------------------------------------------------------------------

    def resolve(
        self,
        action_id: str,
        *,
        actor: str,
        request_id: str | None = None,
    ) -> RecoveryAction:
        """Re-query gateway truth for an UNKNOWN action (GETs only — safe).

        RECOVERED requires positive gateway evidence (order paid, or the
        linked payment captured). Anything else leaves the action UNKNOWN —
        surfaced, never guessed.
        """
        rid = self._rid(request_id)
        action = self._db.get(RecoveryAction, action_id)
        if action is None:
            raise RecoveryNotFoundError(f"recovery action not found: {action_id!r}")
        if action.status is not RecoveryStatus.UNKNOWN:
            raise InvalidStateError(
                f"action {action.id} is {action.status.value}, not UNKNOWN; "
                "nothing to resolve"
            )

        evidence: dict[str, Any] = {}
        gw = self._gateway_for(action.opportunity)  # real_test re-queries the real adapter
        try:
            # Path 1: the entity this action created (id captured before the
            # outcome was lost — rare, but then it is decisive).
            created_id = (action.gateway_response or {}).get("id")
            if action.action_type is ActionType.RETRY_PAYMENT and created_id:
                order = gw.fetch_order(created_id)
                evidence["order_status"] = order.get("status")
                evidence["order_amount_paid"] = order.get("amount_paid")
                if order.get("id") != created_id:
                    # Identity confusion: a status answered for an entity we
                    # did not ask about proves NOTHING — stay UNKNOWN.
                    evidence["order_id_mismatch"] = order.get("id")
                elif order.get("status") == "paid" or (order.get("amount_paid") or 0) >= action.amount_paise:
                    return self._resolve_recovered(
                        action, actor=actor, request_id=rid, source="fetch_order", evidence=evidence
                    )

            # Path 2: the original failed payment's gateway truth.
            payment = self._linked_payment(action)
            if payment is not None and payment.gateway_payment_id:
                try:
                    remote = gw.fetch_payment(payment.gateway_payment_id)
                except GatewayNotFoundError:
                    evidence["linked_payment"] = "not_found_at_gateway"
                else:
                    evidence["linked_payment_status"] = remote.get("status")
                    if remote.get("id") != payment.gateway_payment_id:
                        # Identity confusion — see above; never recover on it.
                        evidence["linked_payment_id_mismatch"] = remote.get("id")
                    elif remote.get("captured") or remote.get("status") == "captured":
                        return self._resolve_recovered(
                            action, actor=actor, request_id=rid, source="fetch_payment", evidence=evidence
                        )
        except GatewayTransientError as exc:
            action.last_error = f"resolve re-query inconclusive: {exc}"
            evidence["error"] = str(exc)

        entry = audit.record(
            self._db,
            actor=actor,
            action="recovery.action.resolve_check",
            entity_type="recovery_action",
            entity_id=action.id,
            details={"result": "still_unknown", **evidence},
            request_id=rid,
        )
        entry.environment = action.environment or ENVIRONMENT_RESEARCH
        logger.info(
            "recovery action still UNKNOWN after re-query",
            extra={"action_id": action.id, "evidence": evidence},
        )
        return action

    # ------------------------------------------------------------------
    # internals: action creation + policy gate
    # ------------------------------------------------------------------

    def _pick_strategy(
        self, opp: RecoveryOpportunity, strategy_id: str | None
    ) -> RecoveryStrategy:
        if strategy_id:
            strategy = self._db.get(RecoveryStrategy, strategy_id)
            if strategy is None or strategy.opportunity_id != opp.id:
                raise RecoveryNotFoundError(
                    f"strategy {strategy_id!r} not found for opportunity {opp.id}"
                )
            if not strategy.eligibility:
                raise InvalidStateError(
                    f"strategy {strategy.id} ({strategy.action_type.value}) is "
                    f"ineligible: {strategy.reason}"
                )
            return strategy
        recommended = self._strategies.recommended_for(opp.id)
        if recommended is None:
            # Generate the candidate set on first use, then take the winner.
            self._strategies.generate(opp)
            recommended = self._strategies.recommended_for(opp.id)
        if recommended is None:
            raise InvalidStateError(
                f"opportunity {opp.id} has no eligible strategy to execute"
            )
        return recommended

    def _create_action(
        self,
        opp: RecoveryOpportunity,
        strategy: RecoveryStrategy,
        *,
        actor: str,
        request_id: str | None,
    ) -> RecoveryAction:
        action = RecoveryAction(
            opportunity_id=opp.id,
            strategy_id=strategy.id,
            incident_id=opp.incident_id,
            action_type=strategy.action_type,
            status=RecoveryStatus.PROPOSED,
            amount_paise=opp.amount_paise,
            currency=opp.currency or "INR",
            confidence=strategy.confidence,
            # Actions inherit the opportunity's environment — gateway routing
            # and read-API scoping both key off exactly this stamp.
            environment=opp.environment or ENVIRONMENT_RESEARCH,
            # Idempotency key for the gateway call; unique column. 36 chars,
            # inside Razorpay's 40-char receipt/reference_id limit.
            gateway_request_id=ids.new_id("gwr_"),
            actor=actor,
            proposed_at=self._clock(),
        )
        self._db.add(action)
        self._db.flush()
        entry = audit.record(
            self._db,
            actor=actor,
            action="recovery.action.proposed",
            entity_type="recovery_action",
            entity_id=action.id,
            details={
                "from_status": None,
                "to_status": RecoveryStatus.PROPOSED.value,
                "opportunity_id": opp.id,
                "strategy_id": strategy.id,
                "action_type": action.action_type.value,
                "amount_paise": action.amount_paise,
                "constraints": dict(strategy.constraints or {}),
            },
            request_id=request_id,
        )
        entry.environment = action.environment
        self._sync_opportunity(opp, action)
        return action

    def _gate(
        self, action: RecoveryAction, *, actor: str, request_id: str | None
    ) -> PolicyDecision:
        """Run the deterministic policy gate and persist the decision link."""
        opp = action.opportunity
        customer = (
            self._db.get(Customer, opp.customer_id) if opp.customer_id else None
        )
        attempts_so_far = self._prior_attempts(action)
        consecutive_failures = (
            self._history.consecutive_failed_for_incident(
                action.incident_id, exclude_action_id=action.id
            )
            if action.incident_id
            else 0
        )
        ctx = ActionContext(
            action_type=action.action_type,
            amount_paise=action.amount_paise,
            confidence=action.confidence,
            actor=action.actor or actor,
            currency=action.currency or "INR",
            incident_id=action.incident_id,
            opportunity_id=action.opportunity_id,
            customer_id=opp.customer_id,
            customer_opted_out=bool(customer.opted_out) if customer else False,
            attempts_so_far=attempts_so_far,
            consecutive_failures=consecutive_failures,
            metadata={
                META_CURRENT_ACTION_ID: action.id,
                META_STRATEGY_ID: action.strategy_id or "",
                META_REQUEST_ID: request_id or "",
            },
        )
        decision = self._policy.evaluate(ctx)  # persists PolicyDecisionRecord
        record = self.latest_policy_decision(action)
        if record is not None:
            action.policy_decision_id = record.id
        action.decided_at = self._clock()
        self._transition(
            action,
            RecoveryStatus.POLICY_EVALUATED,
            actor=actor,
            request_id=request_id,
            details={
                "policy_outcome": decision.outcome.value,
                "policy_decision_id": action.policy_decision_id,
                "rules_matched": decision.rules_matched,
                "policy_version": decision.policy_version,
            },
        )
        return decision

    def _prior_attempts(self, action: RecoveryAction) -> int:
        stmt = (
            sa.select(sa.func.count())
            .select_from(RecoveryAction)
            .where(
                RecoveryAction.opportunity_id == action.opportunity_id,
                RecoveryAction.id != action.id,
                RecoveryAction.status.notin_(
                    (RecoveryStatus.REJECTED, RecoveryStatus.CANCELLED)
                ),
            )
        )
        return int(self._db.execute(stmt).scalar_one())

    # ------------------------------------------------------------------
    # internals: gateway fire + verification
    # ------------------------------------------------------------------

    def _fire(
        self,
        action: RecoveryAction,
        *,
        actor: str,
        request_id: str | None,
        from_scheduled: bool = False,
    ) -> None:
        opp = action.opportunity
        if (
            not from_scheduled
            and self._delay_seconds(action) > 0
            and not self.scheduled_due(action)
        ):
            # Delayed retry whose wait has not elapsed: park in SCHEDULED
            # without consuming an attempt or touching the gateway. The
            # worker fires it through execute() once due (docs/worker.md);
            # from_scheduled marks exactly that fire-through path — the
            # action was parked and came due — so it must NOT re-park here.
            self._park(action, actor=actor, request_id=request_id)
            return
        action.attempts += 1
        action.executed_at = self._clock()
        self._transition(
            action,
            RecoveryStatus.EXECUTING,
            actor=actor,
            request_id=request_id,
            details={"gateway_request_id": action.gateway_request_id},
        )
        # Safe, gateway-free actions terminate immediately and truthfully.
        if action.action_type is ActionType.ESCALATE_HUMAN:
            self._transition(
                action,
                RecoveryStatus.ESCALATED,
                actor=actor,
                request_id=request_id,
                note="handed off to a human operator",
                details={"handoff": "human_ops"},
            )
            return
        if action.action_type is ActionType.NO_ACTION:
            self._transition(
                action,
                RecoveryStatus.CANCELLED,
                actor=actor,
                request_id=request_id,
                note="no_action executed: nothing to do, by design",
            )
            return
        try:
            response = self._dispatch_gateway(action, opp, actor=actor, request_id=request_id)
        except GatewayClientError as exc:
            # 4xx: rejected before processing — definitively nothing happened.
            self._transition(
                action,
                RecoveryStatus.FAILED,
                actor=actor,
                request_id=request_id,
                error=f"{type(exc).__name__}: {exc}",
                details={"gateway_status_code": exc.status_code, "gateway_reason": exc.reason},
            )
            return
        except GatewayTransientError as exc:
            # Timeout / 5xx / unreadable response: the request MAY have been
            # processed. UNKNOWN — no blind retry; resolve() re-queries.
            self._transition(
                action,
                RecoveryStatus.UNKNOWN,
                actor=actor,
                request_id=request_id,
                error=f"{type(exc).__name__}: {exc}",
                details={
                    "ambiguous_outcome": True,
                    "resolution": "re-query via fetch_payment/fetch_order; never blind-retry",
                },
            )
            return

        action.gateway_response = response
        self._transition(
            action,
            RecoveryStatus.VERIFYING,
            actor=actor,
            request_id=request_id,
            details={"gateway_entity_id": response.get("id")},
        )
        self._verify_inline(action, response, actor=actor, request_id=request_id)

    def _dispatch_gateway(
        self,
        action: RecoveryAction,
        opp: RecoveryOpportunity,
        *,
        actor: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Map the action type to exactly one gateway mutation (or none).

        The gateway is chosen by the opportunity's environment: research ->
        the injected (simulated) twin; real_test -> the real Razorpay adapter
        or an honest razorpay_not_configured refusal (raised BEFORE any
        mutation attempt). `_fire` always passes the executing actor; the
        None defaults keep the dispatcher directly probeable (invariant
        tests) without forging attribution."""
        gw = self._gateway_for(opp)
        if action.action_type is ActionType.RETRY_PAYMENT:
            # Razorpay has no "retry" call: a fresh order with our idempotency
            # key as receipt gives the customer a new payable attempt.
            notes: dict[str, Any] = {
                "recovery_action_id": action.id,
                "original_payment_id": opp.payment_id or "",
            }
            delay = self._delay_seconds(action)
            if delay:
                notes["requested_delay_seconds"] = str(delay)
            return gw.create_order(
                amount_paise=action.amount_paise,
                currency=action.currency or "INR",
                idempotency_key=action.gateway_request_id,
                notes=notes,
            )
        if action.action_type is ActionType.CREATE_PAYMENT_LINK:
            customer = (
                self._db.get(Customer, opp.customer_id) if opp.customer_id else None
            )
            customer_payload = None
            if customer is not None:
                customer_payload = {
                    k: v
                    for k, v in {
                        "name": customer.name,
                        "email": customer.email,
                        "contact": customer.phone,
                    }.items()
                    if v
                } or None
            return gw.create_payment_link(
                amount_paise=action.amount_paise,
                currency=action.currency or "INR",
                customer=customer_payload,
                description=f"PulseRecover recovery for opportunity {opp.id}",
                idempotency_key=action.gateway_request_id,
            )
        if action.action_type is ActionType.NOTIFY_CUSTOMER:
            # No money moves: the notification is queued in the outbox and the
            # worker delivers it via the NotificationSender port
            # (docs/worker.md). Recovery is verified when the customer pays
            # (webhook on the linked payment moves VERIFYING -> RECOVERED).
            outbox = self._enqueue_notification(
                action,
                opp,
                actor=actor or action.actor or "system:executor",
                request_id=request_id,
            )
            return {
                "id": action.gateway_request_id,
                "entity": "notification",
                "notified": True,
                "channel": outbox.channel,
                "outbox_id": outbox.id,
            }
        raise GatewayClientError(  # definitive, no side effects
            f"no executor mapping for action type {action.action_type.value!r}; "
            "only retry_payment, create_payment_link and notify_customer execute",
            status_code=None,
            code="UNSUPPORTED_ACTION",
        )

    def _verify_inline(
        self,
        action: RecoveryAction,
        response: dict[str, Any],
        *,
        actor: str,
        request_id: str | None,
    ) -> None:
        """Synchronous verification when the gateway response is decisive
        (simulator pays links inline; real Razorpay resolves via webhook)."""
        paid = False
        if action.action_type is ActionType.CREATE_PAYMENT_LINK:
            paid = response.get("status") == "paid" or (
                (response.get("amount_paid") or 0) >= action.amount_paise
                and action.amount_paise > 0
            )
        elif action.action_type is ActionType.RETRY_PAYMENT:
            paid = response.get("status") == "paid" or (
                (response.get("amount_paid") or 0) >= action.amount_paise
                and action.amount_paise > 0
            )
        # notify_customer can never verify inline: VERIFYING until the
        # customer's payment webhook lands.
        if paid:
            self._transition(
                action,
                RecoveryStatus.RECOVERED,
                actor=actor,
                request_id=request_id,
                details={"verification": "inline_gateway_response"},
            )

    def _resolve_recovered(
        self,
        action: RecoveryAction,
        *,
        actor: str,
        request_id: str | None,
        source: str,
        evidence: dict[str, Any],
    ) -> RecoveryAction:
        self._transition(
            action,
            RecoveryStatus.RECOVERED,
            actor=actor,
            request_id=request_id,
            details={"verification": source, **evidence},
        )
        return action

    def _delay_seconds(self, action: RecoveryAction) -> int:
        """Requested delay for delayed-retry strategies. Honored by parking
        the action in SCHEDULED until due (`_park`); also recorded in the
        gateway order's notes as part of the audited proposal."""
        if not action.strategy_id:
            return 0
        strategy = self._db.get(RecoveryStrategy, action.strategy_id)
        if strategy is None:
            return 0
        try:
            return max(0, int((strategy.constraints or {}).get("delay_seconds", 0)))
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # internals: delayed-retry scheduling (SCHEDULED; docs/worker.md)
    # ------------------------------------------------------------------

    def _scheduled_anchor(self, action: RecoveryAction) -> datetime:
        """The timestamp the delay counts from: the latest policy decision
        (re-gating restarts the wait honestly), proposed_at as fallback."""
        since = action.decided_at or action.proposed_at
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return since

    def scheduled_due_at(self, action: RecoveryAction) -> datetime | None:
        """When a delayed retry comes due, or None when no delay is requested."""
        delay = self._delay_seconds(action)
        if delay <= 0:
            return None
        return self._scheduled_anchor(action) + timedelta(seconds=delay)

    def scheduled_due(self, action: RecoveryAction, *, now: datetime | None = None) -> bool:
        """True when the action's requested delay has elapsed. Actions with no
        delay requested are always due. The worker passes its (injected)
        clock as `now`; the executor's own clock is the default."""
        due = self.scheduled_due_at(action)
        if due is None:
            return True
        return (now or self._clock()) >= due

    def _park(self, action: RecoveryAction, *, actor: str, request_id: str | None) -> None:
        """Park a delayed retry in SCHEDULED until due. No attempt is consumed
        and nothing reaches the gateway: the state is pre-execution, so it
        stays cancellable and occupies the opportunity's execution slot."""
        delay = self._delay_seconds(action)
        due = self.scheduled_due_at(action)
        self._transition(
            action,
            RecoveryStatus.SCHEDULED,
            actor=actor,
            request_id=request_id,
            note=f"delayed retry parked for {delay}s; the worker fires it when due",
            details={
                "delay_seconds": delay,
                "due_at": due.isoformat() if due is not None else None,
                "fires_via": "worker",
            },
        )

    def _enqueue_notification(
        self,
        action: RecoveryAction,
        opp: RecoveryOpportunity,
        *,
        actor: str,
        request_id: str | None,
    ) -> NotificationOutbox:
        """Queue a notify_customer contact in the outbox (PENDING, due now);
        the worker delivers it via the NotificationSender port. Enqueued once
        per action — the action fires exactly once, ever."""
        channel = "notification"
        if action.strategy_id:
            strategy = self._db.get(RecoveryStrategy, action.strategy_id)
            if strategy is not None:
                try:
                    channel = str((strategy.constraints or {}).get("channel") or "notification")
                except (TypeError, AttributeError):
                    channel = "notification"
        customer = self._db.get(Customer, opp.customer_id) if opp.customer_id else None
        customer_payload = None
        if customer is not None:
            customer_payload = {
                k: v
                for k, v in {
                    "id": customer.id,
                    "name": customer.name,
                    "email": customer.email,
                    "contact": customer.phone,
                }.items()
                if v
            } or None
        row = NotificationOutbox(
            action_id=action.id,
            customer_id=opp.customer_id,
            channel=channel,
            payload={
                "opportunity_id": opp.id,
                "action_id": action.id,
                "incident_id": action.incident_id,
                "amount_paise": action.amount_paise,
                "currency": action.currency or "INR",
                "customer": customer_payload,
                "message": (
                    "PulseRecover: your payment did not complete — "
                    "please retry at your convenience."
                ),
            },
            status=NotificationStatus.PENDING,
            attempts=0,
            due_at=self._clock(),
            environment=action.environment or ENVIRONMENT_RESEARCH,
        )
        self._db.add(row)
        self._db.flush()
        entry = audit.record(
            self._db,
            actor=actor,
            action="notification.queued",
            entity_type="notification_outbox",
            entity_id=row.id,
            details={
                "recovery_action_id": action.id,
                "opportunity_id": opp.id,
                "customer_id": opp.customer_id,
                "channel": channel,
            },
            request_id=request_id,
        )
        entry.environment = row.environment
        return row

    def _linked_payment(self, action: RecoveryAction) -> Payment | None:
        opp = action.opportunity
        if opp is None or not opp.payment_id:
            return None
        return self._db.get(Payment, opp.payment_id)

    # ------------------------------------------------------------------
    # internals: transitions + audit
    # ------------------------------------------------------------------

    def _transition(
        self,
        action: RecoveryAction,
        to: RecoveryStatus,
        *,
        actor: str,
        request_id: str | None,
        note: str | None = None,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        now = self._clock()
        frm = action.status
        action.status = to
        if note is not None:
            action.note = note
        if error is not None:
            action.last_error = error
        if to is RecoveryStatus.RECOVERED:
            action.verified_at = now
        if to in TERMINAL_STATES or to is RecoveryStatus.RECOVERED:
            action.completed_at = now
        self._db.flush()
        entry = audit.record(
            self._db,
            actor=actor,
            action=f"recovery.action.{to.value.lower()}",
            entity_type="recovery_action",
            entity_id=action.id,
            details={
                "from_status": frm.value if frm else None,
                "to_status": to.value,
                **(details or {}),
            },
            request_id=request_id,
        )
        entry.environment = action.environment or ENVIRONMENT_RESEARCH
        if action.opportunity is not None:
            self._sync_opportunity(action.opportunity, action)
        logger.info(
            "recovery action transition",
            extra={
                "action_id": action.id,
                "from_status": frm.value if frm else None,
                "to_status": to.value,
                "actor": actor,
            },
        )

    def _opportunity_level(
        self,
        opp: RecoveryOpportunity,
        to: RecoveryStatus,
        *,
        actor: str,
        request_id: str | None,
        note: str | None,
    ) -> None:
        """Reject/escalate/cancel an opportunity that has no action yet."""
        existing = self._db.scalar(
            sa.select(sa.func.count())
            .select_from(RecoveryAction)
            .where(RecoveryAction.opportunity_id == opp.id)
        )
        if existing:
            raise InvalidStateError(
                f"opportunity {opp.id} has no open action (all terminal); "
                "there is nothing left to transition"
            )
        frm = opp.status
        opp.status = to
        self._db.flush()
        entry = audit.record(
            self._db,
            actor=actor,
            action=f"recovery.opportunity.{to.value.lower()}",
            entity_type="recovery_opportunity",
            entity_id=opp.id,
            details={
                "from_status": frm.value if frm else None,
                "to_status": to.value,
                "note": note,
            },
            request_id=request_id,
        )
        entry.environment = opp.environment or ENVIRONMENT_RESEARCH

    def _sync_opportunity(
        self, opp: RecoveryOpportunity, action: RecoveryAction
    ) -> None:
        """Opportunity status shadows its latest action (1:1 rollup for the
        dashboard status pill)."""
        opp.status = action.status
        self._db.flush()

    @staticmethod
    def _rid(request_id: str | None) -> str | None:
        return request_id or request_id_ctx.get()


__all__ = [
    "APPROVAL_TTL_ACTOR",
    "APPROVAL_TTL_RULE",
    "CANCELLABLE_STATES",
    "GatewayNotConfiguredError",
    "IN_FLIGHT_STATES",
    "InvalidStateError",
    "OPEN_STATES",
    "RecoveryError",
    "RecoveryExecutor",
    "RecoveryNotFoundError",
    "TERMINAL_STATES",
]

"""Recovery executor: the closed-loop state machine for recovery_actions.

    PROPOSED -> POLICY_EVALUATED -> ALLOWED          -> auto-execute
                                 -> REQUIRES_APPROVAL -> PENDING_APPROVAL
                                 -> BLOCKED           -> REJECTED (terminal)
    PENDING_APPROVAL --approve--> APPROVED -> execute
    PENDING_APPROVAL --reject-->  REJECTED (terminal)
    any pre-execution state --cancel--> CANCELLED (terminal)
    any non-terminal state --escalate--> ESCALATED (terminal)
    EXECUTING -> VERIFYING -> RECOVERED | FAILED
    EXECUTING -> UNKNOWN   (gateway gave no authoritative answer)

Safety invariants (enforced here, proven by tests/recovery):
- EVERY execution passes PolicyEngine.evaluate first — AI output is advisory.
- One gateway mutation per action, ever: `gateway_request_id` is the
  idempotency key (mapped to Razorpay `receipt` / `reference_id`), and a
  second execute on the same opportunity reuses the open action instead of
  creating a new one. Cross-opportunity duplicates are BLOCKED by the policy
  gate's duplicate-protection guard.
- GatewayTransientError (timeout / 5xx / unreadable response) -> UNKNOWN.
  NEVER blind-retry a mutating call; resolve by re-querying gateway truth
  (fetch_payment / fetch_order) via `resolve()`.
- GatewayClientError (4xx) -> FAILED: the gateway rejected the request before
  processing it, so nothing happened — a definitive, truthful failure.
- Every state transition appends an audit_logs row with actor + request_id.

Transaction boundary: this service flushes but NEVER commits (same convention
as the policy engine and audit helper); the API layer commits.
"""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import ids
from app.db import utcnow
from app.logging import get_logger, request_id_ctx
from app.models import (
    Customer,
    Payment,
    PolicyDecisionRecord,
    RecoveryAction,
    RecoveryOpportunity,
    RecoveryStrategy,
)
from app.ports import (
    ActionContext,
    ActionType,
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
)
TERMINAL_STATES = (
    RecoveryStatus.RECOVERED,
    RecoveryStatus.FAILED,
    RecoveryStatus.REJECTED,
    RecoveryStatus.CANCELLED,
    RecoveryStatus.ESCALATED,
)


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


class RecoveryExecutor:
    """Drives recovery actions through policy, gateway, and verification."""

    def __init__(
        self,
        session: Session,
        gateway: PaymentGateway,
        *,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self._db = session
        self._gw = gateway
        self._policy = policy_engine or PolicyEngine.from_file(session=session)
        self._history = SqlPolicyHistory(session)
        self._strategies = StrategyGenerator(session)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get_opportunity(self, opportunity_id: str) -> RecoveryOpportunity:
        opp = self._db.get(RecoveryOpportunity, opportunity_id)
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
        return self._db.scalar(stmt)

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
          run it through the policy gate; ALLOWED fires immediately.
        - Open action PENDING_APPROVAL: refuse — a human must approve first.
        - Open action EXECUTING/VERIFYING: refuse — a gateway call is live.
        - Open action UNKNOWN: NO blind retry — re-query gateway truth instead.
        """
        rid = self._rid(request_id)
        opp = self.get_opportunity(opportunity_id)
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
        self._fire(action, actor=actor, request_id=rid)
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
        opp = self.get_opportunity(opportunity_id)
        action = self.open_action_for(opp.id)
        if action is None or action.status is not RecoveryStatus.PENDING_APPROVAL:
            raise InvalidStateError(
                f"opportunity {opp.id} has no action awaiting approval"
            )
        action.approved_at = utcnow()
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
        opp = self.get_opportunity(opportunity_id)
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
        opp = self.get_opportunity(opportunity_id)
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
        opp = self.get_opportunity(opportunity_id)
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
        try:
            # Path 1: the entity this action created (id captured before the
            # outcome was lost — rare, but then it is decisive).
            created_id = (action.gateway_response or {}).get("id")
            if action.action_type is ActionType.RETRY_PAYMENT and created_id:
                order = self._gw.fetch_order(created_id)
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
                    remote = self._gw.fetch_payment(payment.gateway_payment_id)
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

        audit.record(
            self._db,
            actor=actor,
            action="recovery.action.resolve_check",
            entity_type="recovery_action",
            entity_id=action.id,
            details={"result": "still_unknown", **evidence},
            request_id=rid,
        )
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
            # Idempotency key for the gateway call; unique column. 36 chars,
            # inside Razorpay's 40-char receipt/reference_id limit.
            gateway_request_id=ids.new_id("gwr_"),
            actor=actor,
            proposed_at=utcnow(),
        )
        self._db.add(action)
        self._db.flush()
        audit.record(
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
        action.decided_at = utcnow()
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

    def _fire(self, action: RecoveryAction, *, actor: str, request_id: str | None) -> None:
        opp = action.opportunity
        action.attempts += 1
        action.executed_at = utcnow()
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
            response = self._dispatch_gateway(action, opp)
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
        self, action: RecoveryAction, opp: RecoveryOpportunity
    ) -> dict[str, Any]:
        """Map the action type to exactly one gateway mutation (or none)."""
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
            return self._gw.create_order(
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
            return self._gw.create_payment_link(
                amount_paise=action.amount_paise,
                currency=action.currency or "INR",
                customer=customer_payload,
                description=f"PulseRecover recovery for opportunity {opp.id}",
                idempotency_key=action.gateway_request_id,
            )
        if action.action_type is ActionType.NOTIFY_CUSTOMER:
            # No money moves and this monolith has no notification worker; the
            # contact is recorded and recovery is verified when the customer
            # pays (webhook on the linked payment moves VERIFYING -> RECOVERED).
            return {
                "id": action.gateway_request_id,
                "entity": "notification",
                "notified": True,
                "channel": "recorded",
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
        """Requested delay for delayed-retry strategies (recorded in gateway
        notes; the monolith has no scheduler, so execution is immediate)."""
        if not action.strategy_id:
            return 0
        strategy = self._db.get(RecoveryStrategy, action.strategy_id)
        if strategy is None:
            return 0
        try:
            return max(0, int((strategy.constraints or {}).get("delay_seconds", 0)))
        except (TypeError, ValueError):
            return 0

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
        now = utcnow()
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
        audit.record(
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
        audit.record(
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
    "CANCELLABLE_STATES",
    "IN_FLIGHT_STATES",
    "InvalidStateError",
    "OPEN_STATES",
    "RecoveryError",
    "RecoveryExecutor",
    "RecoveryNotFoundError",
    "TERMINAL_STATES",
]

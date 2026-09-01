"""Agent tool layer — the ONLY way a reasoner (heuristic or LLM) may touch
data or propose mutations.

Trust boundary (ADR 0003/0004):
- Read tools answer questions from the deterministic services and the shared
  models. Every result carries ``evidence_ids`` (DB row ids) so any claim a
  reasoner makes is traceable back to concrete rows.
- ALL financial facts originate here, never from the reasoner. The LLM sees
  these numbers and may cite them; the hallucination guard
  (``app.services.agent.validation``) rejects any number it invented.
- The two ``request_*`` tools are the ONLY mutation path. They create a
  ``recovery_actions`` row in status PROPOSED — with the amount copied from
  the ORIGINAL payment/opportunity row, never from caller input — and then
  evaluate the proposal through the deterministic PolicyEngine, returning the
  decision verbatim. They NEVER call the payment gateway. Execution belongs
  to the recovery executor, which re-checks policy and approval state.
- Reasoners can only invoke whitelisted tool names through ``call()``; there
  is no getattr/arbitrary-callable path.

Money convention: integer paise + "INR" everywhere.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.logging import get_logger
from app.models import (
    Customer,
    Diagnosis,
    Incident,
    ModelPrediction,
    Payment,
    PolicyDecisionRecord,
    RecoveryAction,
    RecoveryOpportunity,
)
from app.ports import ActionContext, ActionType, PolicyDecision, RecoveryStatus
from app.services.policy import audit
from app.services.policy.engine import META_CURRENT_ACTION_ID, PolicyEngine
from app.services.revenue.classify import classify_failure
from app.services.revenue.engine import RevenueService

log = get_logger(__name__)

#: Actor identity stamped on rows the investigator creates.
AGENT_ACTOR = "agent:investigator"

#: Opportunity type used when the investigator derives a candidate from a
#: failed payment that no recovery-opportunity row exists for yet.
DERIVED_OPPORTUNITY_TYPE = "failed_payment_retry"

#: Terminal action statuses — actions in these states no longer count as
#: "prior attempts" when a new request is evaluated.
_TERMINAL_ACTION_STATUSES = (
    RecoveryStatus.REJECTED,
    RecoveryStatus.CANCELLED,
    RecoveryStatus.RECOVERED,
)


class ToolError(RuntimeError):
    """A tool was called with bad arguments or a missing target."""


class ToolNotAllowed(ToolError):
    """A reasoner tried to invoke a name outside the tool whitelist."""


@dataclass(frozen=True)
class ToolResult:
    """One tool invocation. ``data`` is JSON-safe; ``evidence_ids`` lists the
    DB row ids the data was derived from (traceability contract)."""

    name: str
    data: dict[str, Any]
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "data": self.data, "evidence_ids": list(self.evidence_ids)}


def _decision_dict(decision: PolicyDecision) -> dict[str, Any]:
    """The policy outcome, verbatim. Deliberately timestamp-free so tool
    output stays deterministic for a given DB/policy state."""
    return {
        "outcome": decision.outcome.value,
        "reasons": list(decision.reasons),
        "rules_matched": list(decision.rules_matched),
        "policy_version": decision.policy_version,
    }


def _valid_confidence(confidence: float | None) -> float:
    """Normalize a caller-supplied confidence for a MUTATION tool.

    NaN/Infinity would crash the INSERT (SQLite binds NaN as NULL into the
    NOT NULL confidence column) before the policy gate ever saw it; values
    outside [0, 1] are malformed input. Mutation tools fail closed with a
    ToolError BEFORE any row is created — the dry-run
    ``propose_recovery_strategy`` instead passes such values through to the
    gate, which BLOCKs them as ``malformed.confidence``.
    """
    conf = 0.5 if confidence is None else float(confidence)
    if not math.isfinite(conf) or not 0.0 <= conf <= 1.0:
        raise ToolError(
            f"invalid confidence {confidence!r}: must be a finite number in [0, 1]"
        )
    return conf


class AgentTools:
    """Typed tools over the deterministic services and the DB.

    One instance is bound to one incident investigation. ``calls`` accumulates
    every ToolResult produced through this instance — the reasoners and the
    hallucination guard consume that log.
    """

    def __init__(
        self,
        session: Session,
        *,
        incident_id: str,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self._session = session
        self._incident_id = incident_id
        if policy_engine is not None:
            self._engine = policy_engine
        else:
            try:
                self._engine = PolicyEngine.from_file(session=session)
            except Exception as exc:  # fail closed, never fail the investigation
                log.warning("policy config unavailable; using failsafe engine", extra={"error": str(exc)})
                self._engine = PolicyEngine.failsafe(str(exc), session=session)
        self.calls: list[ToolResult] = []

    # ------------------------------------------------------------------
    # dispatch (whitelist)
    # ------------------------------------------------------------------

    def _registry(self) -> dict[str, Callable[..., ToolResult]]:
        return {
            "get_incident": self.get_incident,
            "get_payment_stats": self.get_payment_stats,
            "get_failure_distribution": self.get_failure_distribution,
            "get_customer_history": self.get_customer_history,
            "get_revenue_at_risk": self.get_revenue_at_risk,
            "get_recovery_candidates": self.get_recovery_candidates,
            "propose_recovery_strategy": self.propose_recovery_strategy,
            "request_payment_link": self.request_payment_link,
            "request_recovery_execution": self.request_recovery_execution,
        }

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._registry())

    def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        """Invoke a whitelisted tool by name. Anything else raises
        ToolNotAllowed — this is the tool-restriction guard."""
        fn = self._registry().get(name)
        if fn is None:
            raise ToolNotAllowed(
                f"tool {name!r} is not on the agent whitelist "
                f"(allowed: {', '.join(self.tool_names)})"
            )
        result = fn(**self._coerce_args(name, args or {}))
        self.calls.append(result)
        return result

    @staticmethod
    def _coerce_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise ToolError(f"tool {name}: arguments must be an object, got {type(args).__name__}")
        return {str(k): v for k, v in args.items()}

    def specs(self) -> list[dict[str, Any]]:
        """OpenAI-compatible function specs for the LLM tool-calling loop."""
        no_args: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
        target_args = {
            "type": "object",
            "properties": {
                "payment_id": {"type": "string", "description": "Failed payment id (pay_...) from a tool result"},
                "opportunity_id": {"type": "string", "description": "Recovery opportunity id (opp_...) from a tool result"},
                "confidence": {"type": "number", "description": "Your confidence 0..1 that this action is appropriate"},
                "note": {"type": "string", "description": "Short human-readable rationale"},
            },
            "additionalProperties": False,
        }
        strategy_args = {
            "type": "object",
            "properties": {
                "action_type": {
                    "type": "string",
                    "enum": [a.value for a in ActionType],
                    "description": "Proposed action type",
                },
                **target_args["properties"],
            },
            "required": ["action_type"],
            "additionalProperties": False,
        }
        defs = [
            ("get_incident", "Incident row: status, severity, metric, deviation, window, revenue_at_risk, latest diagnosis.", no_args),
            ("get_payment_stats", "Payment counts/amounts and failure rates in the incident window vs the baseline window.", no_args),
            ("get_failure_distribution", "Failed payments in the incident window grouped by error source, reason, method, and failure class.", no_args),
            ("get_customer_history", "Payment history and opt-out flag for one customer id. Call at most once per investigation, for the customer behind your chosen recovery target.", {
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"],
                "additionalProperties": False,
            }),
            ("get_revenue_at_risk", "Counterfactual revenue-at-risk analysis (observed loss, recoverable, expected recovery per strategy, actual recovered).", no_args),
            ("get_recovery_candidates", "Recovery opportunities for this incident (existing rows, or derived from failed payments when none exist).", no_args),
            ("propose_recovery_strategy", "Dry-run: evaluate a candidate action through the deterministic policy engine. No row is created, nothing executes. Call at most once, for your single recommended_next_step — the system attaches its outcome to your report.", strategy_args),
            ("request_payment_link", "Create a PROPOSED create_payment_link recovery action for a failed payment and evaluate it through policy. The amount comes from the original payment. Never executes.", target_args),
            ("request_recovery_execution", "Create a PROPOSED recovery action of the given type and evaluate it through policy. The amount comes from the original payment. Never executes.", strategy_args),
        ]
        return [
            {
                "type": "function",
                "function": {"name": name, "description": desc, "parameters": params},
            }
            for name, desc, params in defs
        ]

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _incident(self) -> Incident:
        incident = self._session.get(Incident, self._incident_id)
        if incident is None:
            raise ToolError(f"incident not found: {self._incident_id!r}")
        return incident

    def _window(self, incident: Incident) -> tuple[Any, Any]:
        end = incident.window_end or incident.detected_at
        start = incident.window_start or (end - timedelta(hours=1))
        return start, end

    def _payments_between(self, start, end) -> list[Payment]:
        stmt = (
            sa.select(Payment)
            .where(Payment.created_at >= start, Payment.created_at < end)
            .order_by(Payment.id)
        )
        return list(self._session.scalars(stmt))

    def _latest_diagnosis(self) -> tuple[Diagnosis | None, dict[str, Any] | None]:
        diag = self._session.scalars(
            sa.select(Diagnosis)
            .where(Diagnosis.incident_id == self._incident_id)
            .order_by(Diagnosis.version.desc())
            .limit(1)
        ).first()
        if diag is None:
            return None, None
        pred = self._session.scalars(
            sa.select(ModelPrediction)
            .where(
                ModelPrediction.incident_id == self._incident_id,
                ModelPrediction.prediction_type == "diagnosis",
            )
            .order_by(ModelPrediction.created_at.desc())
            .limit(1)
        ).first()
        top3 = pred.output.get("top3") if pred and isinstance(pred.output, dict) else None
        heuristic = bool(pred.output.get("heuristic")) if pred and isinstance(pred.output, dict) else None
        ctx = {
            "diagnosis_id": diag.id,
            "label": diag.predicted_cause,
            "confidence": diag.confidence,
            "model_name": diag.model_name,
            "model_version": diag.model_version,
            "top3": top3 or [{"label": diag.predicted_cause, "probability": diag.confidence}],
            "heuristic": heuristic,
            "explanation": diag.explanation,
        }
        return diag, ctx

    # ------------------------------------------------------------------
    # read tools
    # ------------------------------------------------------------------

    def get_incident(self) -> ToolResult:
        inc = self._incident()
        diag, diag_ctx = self._latest_diagnosis()
        data = {
            "incident_id": inc.id,
            "title": inc.title,
            "description": inc.description,
            "status": inc.status.value,
            "severity": inc.severity.value,
            "metric": inc.metric,
            "detection_method": inc.detection_method,
            "baseline_value": inc.baseline_value,
            "observed_value": inc.observed_value,
            "deviation_pct": inc.deviation_pct,
            "window_start": (inc.window_start.isoformat() if inc.window_start else None),
            "window_end": (inc.window_end.isoformat() if inc.window_end else None),
            "detected_at": inc.detected_at.isoformat(),
            "affected_payments_count": inc.affected_payments_count,
            "revenue_at_risk_paise": inc.revenue_at_risk_paise,
            "currency": inc.currency,
            "root_cause": inc.root_cause,
            "diagnosis": diag_ctx,
        }
        evidence = [inc.id] + ([diag.id] if diag else [])
        return ToolResult("get_incident", data, evidence)

    def get_payment_stats(self, sample_limit: int = 25) -> ToolResult:
        inc = self._incident()
        w_start, w_end = self._window(inc)
        duration = w_end - w_start
        window = self._payments_between(w_start, w_end)
        baseline = self._payments_between(w_start - duration, w_start)

        def summarize(payments: list[Payment]) -> dict[str, Any]:
            failed = [p for p in payments if p.status == "failed"]
            captured = [p for p in payments if p.captured or p.status in ("captured", "refunded")]
            resolved = len(failed) + len(captured)
            return {
                "total": len(payments),
                "failed": len(failed),
                "captured": len(captured),
                "pending": len(payments) - resolved,
                "failed_amount_paise": sum(p.amount_paise for p in failed),
                "captured_amount_paise": sum(p.amount_paise for p in captured),
                "failure_rate": round(len(failed) / resolved, 6) if resolved else None,
            }

        w_stats = summarize(window)
        b_stats = summarize(baseline)
        sample = sorted(
            (p for p in window if p.status == "failed"),
            key=lambda p: p.amount_paise,
            reverse=True,
        )[: max(1, min(int(sample_limit), 100))]
        data = {
            "incident_id": inc.id,
            "window_start": w_start.isoformat(),
            "window_end": w_end.isoformat(),
            "baseline_start": (w_start - duration).isoformat(),
            "baseline_end": w_start.isoformat(),
            "window": w_stats,
            "baseline": b_stats,
            "failure_rate_delta": (
                round(w_stats["failure_rate"] - b_stats["failure_rate"], 6)
                if w_stats["failure_rate"] is not None and b_stats["failure_rate"] is not None
                else None
            ),
            "sample_failed_payment_ids": [p.id for p in sample],
        }
        evidence = [p.id for p in sample]
        return ToolResult("get_payment_stats", data, evidence)

    def get_failure_distribution(self) -> ToolResult:
        inc = self._incident()
        w_start, w_end = self._window(inc)
        failed = [
            p
            for p in self._payments_between(w_start, w_end)
            if p.status == "failed"
        ]

        def dist(key_fn) -> dict[str, int]:
            out: dict[str, int] = {}
            for p in failed:
                key = key_fn(p) or "unknown"
                out[key] = out.get(key, 0) + 1
            return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

        by_class = dist(lambda p: classify_failure(p).value)
        dominant_class = next(iter(by_class), None)
        data = {
            "incident_id": inc.id,
            "window_start": w_start.isoformat(),
            "window_end": w_end.isoformat(),
            "failed_count": len(failed),
            "by_error_source": dist(lambda p: p.error_source),
            "by_error_reason": dist(
                lambda p: (p.meta or {}).get("error_reason") or p.error_code
            ),
            "by_method": dist(lambda p: p.method),
            "by_failure_class": by_class,
            "dominant_failure_class": dominant_class,
        }
        return ToolResult("get_failure_distribution", data, [p.id for p in failed])

    def get_customer_history(self, customer_id: str, recent_limit: int = 5) -> ToolResult:
        customer = self._session.get(Customer, str(customer_id))
        if customer is None:
            raise ToolError(f"customer not found: {customer_id!r}")
        payments = list(
            self._session.scalars(
                sa.select(Payment)
                .where(Payment.customer_id == customer.id)
                .order_by(Payment.created_at.desc())
            )
        )
        captured = [p for p in payments if p.captured or p.status in ("captured", "refunded")]
        failed = [p for p in payments if p.status == "failed"]
        recent = payments[: max(1, min(int(recent_limit), 25))]
        data = {
            "customer_id": customer.id,
            "opted_out": bool(customer.opted_out),
            "total_payments": len(payments),
            "captured_count": len(captured),
            "failed_count": len(failed),
            "captured_amount_paise": sum(p.amount_paise for p in captured),
            "recent_payment_ids": [p.id for p in recent],
        }
        return ToolResult("get_customer_history", data, [customer.id] + [p.id for p in recent])

    def get_revenue_at_risk(self) -> ToolResult:
        inc = self._incident()
        report = RevenueService(self._session).revenue_at_risk(inc.id)

        def est(e) -> dict[str, Any]:
            return {
                "point_paise": e.point_paise,
                "lower_paise": e.lower_paise,
                "upper_paise": e.upper_paise,
                "confidence": e.confidence,
                "low_confidence": e.low_confidence,
                "basis": e.basis,
            }

        data = {
            "incident_id": inc.id,
            "currency": report.currency,
            "window_start": report.window_start.isoformat(),
            "window_end": report.window_end.isoformat(),
            "observed_loss": est(report.observed_loss),
            "recoverable": est(report.recoverable),
            "expected_recovery_by_strategy": {
                k: est(v) for k, v in sorted(report.expected_recovery_by_strategy.items())
            },
            "actual_recovered_paise": report.actual_recovered_paise,
            "recovered_actions_count": report.recovered_actions_count,
            "failure_classes": [
                {
                    "failure_class": fc.failure_class,
                    "failed_count": fc.failed_count,
                    "failed_amount_paise": fc.failed_amount_paise,
                    "recoverable": est(fc.recoverable),
                }
                for fc in report.failure_classes
            ],
        }
        return ToolResult("get_revenue_at_risk", data, [inc.id])

    def get_recovery_candidates(self, limit: int = 10) -> ToolResult:
        inc = self._incident()
        limit = max(1, min(int(limit), 50))
        opps = list(
            self._session.scalars(
                sa.select(RecoveryOpportunity)
                .where(RecoveryOpportunity.incident_id == inc.id)
                .order_by(RecoveryOpportunity.amount_paise.desc())
                .limit(limit)
            )
        )
        if opps:
            data = {
                "incident_id": inc.id,
                "source": "recovery_opportunities",
                "candidates": [
                    {
                        "opportunity_id": o.id,
                        "opportunity_type": o.opportunity_type,
                        "status": o.status.value,
                        "payment_id": o.payment_id,
                        "customer_id": o.customer_id,
                        "amount_paise": o.amount_paise,
                        "currency": o.currency,
                        "expected_recovery_paise": o.expected_recovery_paise,
                        "confidence": o.confidence,
                        "risk": o.risk,
                    }
                    for o in opps
                ],
            }
            return ToolResult(
                "get_recovery_candidates", data, [o.id for o in opps]
            )

        # No opportunity rows yet: derive candidates read-only from failed
        # payments in the incident window (nothing is written here).
        w_start, w_end = self._window(inc)
        failed = sorted(
            (
                p
                for p in self._payments_between(w_start, w_end)
                if p.status == "failed"
            ),
            key=lambda p: p.amount_paise,
            reverse=True,
        )[:limit]
        candidates = [
            {
                "opportunity_id": None,
                "opportunity_type": DERIVED_OPPORTUNITY_TYPE,
                "status": None,
                "payment_id": p.id,
                "customer_id": p.customer_id,
                "amount_paise": p.amount_paise,
                "currency": p.currency or "INR",
                "failure_class": classify_failure(p).value,
                "attempts": p.attempts,
            }
            for p in failed
        ]
        data = {
            "incident_id": inc.id,
            "source": "derived_from_failed_payments",
            "candidates": candidates,
        }
        return ToolResult("get_recovery_candidates", data, [p.id for p in failed])

    # ------------------------------------------------------------------
    # policy preview (read-only)
    # ------------------------------------------------------------------

    def _resolve_target(
        self,
        *,
        payment_id: str | None,
        opportunity_id: str | None,
    ) -> tuple[Payment | None, RecoveryOpportunity | None]:
        payment: Payment | None = None
        opp: RecoveryOpportunity | None = None
        if payment_id:
            payment = self._session.get(Payment, str(payment_id))
            if payment is None:
                raise ToolError(f"payment not found: {payment_id!r}")
        if opportunity_id:
            opp = self._session.get(RecoveryOpportunity, str(opportunity_id))
            if opp is None:
                raise ToolError(f"opportunity not found: {opportunity_id!r}")
            if payment is None and opp.payment_id:
                payment = self._session.get(Payment, opp.payment_id)
        if payment is None and opp is None:
            raise ToolError("a payment_id or opportunity_id target is required")
        return payment, opp

    def _build_context(
        self,
        action_type: ActionType,
        *,
        amount_paise: int,
        confidence: float,
        payment: Payment | None,
        opp: RecoveryOpportunity | None,
        current_action_id: str | None,
    ) -> ActionContext:
        customer_id = (payment.customer_id if payment else None) or (
            opp.customer_id if opp else None
        )
        customer = self._session.get(Customer, customer_id) if customer_id else None
        attempts = 0
        if opp is not None:
            attempts = int(
                self._session.scalar(
                    sa.select(sa.func.count())
                    .select_from(RecoveryAction)
                    .where(
                        RecoveryAction.opportunity_id == opp.id,
                        RecoveryAction.status.notin_(_TERMINAL_ACTION_STATUSES),
                    )
                )
                or 0
            )
        metadata: dict[str, Any] = {}
        if current_action_id:
            metadata[META_CURRENT_ACTION_ID] = current_action_id
        return ActionContext(
            action_type=action_type,
            amount_paise=amount_paise,
            confidence=confidence,
            actor=AGENT_ACTOR,
            currency="INR",
            incident_id=self._incident_id,
            opportunity_id=opp.id if opp else None,
            customer_id=customer_id,
            customer_opted_out=bool(customer.opted_out) if customer else False,
            attempts_so_far=attempts,
            consecutive_failures=0,  # the engine's history source reads the DB itself
            metadata=metadata,
        )

    def propose_recovery_strategy(
        self,
        *,
        action_type: str,
        payment_id: str | None = None,
        opportunity_id: str | None = None,
        confidence: float | None = None,
        note: str | None = None,
    ) -> ToolResult:
        """Dry-run policy preview. Creates nothing; never executes."""
        from app.services.policy.engine import SAFE_ACTIONS

        try:
            at = ActionType(str(action_type))
        except ValueError as exc:
            raise ToolError(f"unknown action_type {action_type!r}") from exc
        payment: Payment | None = None
        opp: RecoveryOpportunity | None = None
        if at in SAFE_ACTIONS and not payment_id and not opportunity_id:
            # Safe, non-financial actions need no payment target (amount 0).
            amount = 0
        else:
            payment, opp = self._resolve_target(
                payment_id=payment_id, opportunity_id=opportunity_id
            )
            # Amount ALWAYS from the original payment/opportunity row.
            amount = payment.amount_paise if payment else opp.amount_paise  # type: ignore[union-attr]
        conf = 0.5 if confidence is None else float(confidence)
        ctx = self._build_context(
            at,
            amount_paise=amount,
            confidence=conf,
            payment=payment,
            opp=opp,
            current_action_id=None,
        )
        decision = self._engine.evaluate(ctx)
        evidence = [x for x in (payment.id if payment else None, opp.id if opp else None) if x]
        data = {
            "action_type": at.value,
            "amount_paise": amount,
            "currency": "INR",
            "confidence": conf,
            "payment_id": payment.id if payment else None,
            "opportunity_id": opp.id if opp else None,
            "customer_id": ctx.customer_id,
            "customer_opted_out": ctx.customer_opted_out,
            "attempts_so_far": ctx.attempts_so_far,
            "policy": _decision_dict(decision),
            "executed": False,
        }
        return ToolResult("propose_recovery_strategy", data, evidence)

    # ------------------------------------------------------------------
    # mutation tools (the ONLY mutation path; policy-gated; never execute)
    # ------------------------------------------------------------------

    def request_payment_link(
        self,
        *,
        payment_id: str | None = None,
        opportunity_id: str | None = None,
        confidence: float | None = None,
        note: str | None = None,
    ) -> ToolResult:
        return self._request_action(
            ActionType.CREATE_PAYMENT_LINK,
            payment_id=payment_id,
            opportunity_id=opportunity_id,
            confidence=confidence,
            note=note,
        )

    def request_recovery_execution(
        self,
        *,
        action_type: str,
        payment_id: str | None = None,
        opportunity_id: str | None = None,
        confidence: float | None = None,
        note: str | None = None,
    ) -> ToolResult:
        try:
            at = ActionType(str(action_type))
        except ValueError as exc:
            raise ToolError(f"unknown action_type {action_type!r}") from exc
        return self._request_action(
            at,
            payment_id=payment_id,
            opportunity_id=opportunity_id,
            confidence=confidence,
            note=note,
        )

    def _find_or_create_opportunity(
        self, payment: Payment, action_type: ActionType
    ) -> RecoveryOpportunity:
        opp = self._session.scalars(
            sa.select(RecoveryOpportunity)
            .where(
                RecoveryOpportunity.payment_id == payment.id,
                RecoveryOpportunity.opportunity_type == DERIVED_OPPORTUNITY_TYPE,
                RecoveryOpportunity.status.notin_(
                    (
                        RecoveryStatus.REJECTED,
                        RecoveryStatus.CANCELLED,
                        RecoveryStatus.RECOVERED,
                    )
                ),
            )
            .order_by(RecoveryOpportunity.created_at.desc())
            .limit(1)
        ).first()
        if opp is not None:
            return opp
        opp = RecoveryOpportunity(
            incident_id=self._incident_id,
            payment_id=payment.id,
            customer_id=payment.customer_id,
            opportunity_type=DERIVED_OPPORTUNITY_TYPE,
            status=RecoveryStatus.PROPOSED,
            amount_paise=payment.amount_paise,
            currency=payment.currency or "INR",
            reason=f"derived by {AGENT_ACTOR} from failed payment {payment.id}",
        )
        self._session.add(opp)
        self._session.flush()
        estimate = RevenueService(self._session).opportunity_estimate(opp, action_type=action_type)
        point = estimate.recoverable.point_paise or 0
        eff = estimate.expected_recovery_by_strategy.get(action_type.value)
        opp.expected_recovery_paise = int(eff.point_paise or 0) if eff else 0
        opp.confidence = estimate.recoverable.confidence
        opp.risk = "low" if point else "medium"
        self._session.flush()
        return opp

    def _request_action(
        self,
        action_type: ActionType,
        *,
        payment_id: str | None,
        opportunity_id: str | None,
        confidence: float | None,
        note: str | None,
    ) -> ToolResult:
        """Create a PROPOSED recovery action and gate it through policy.

        The amount is copied from the original payment/opportunity row — the
        caller can never set it. The policy decision is returned verbatim and
        mirrored onto the action row. The gateway is never called from here.
        """
        conf = _valid_confidence(confidence)
        payment, opp = self._resolve_target(
            payment_id=payment_id, opportunity_id=opportunity_id
        )
        if payment is not None:
            amount = payment.amount_paise
            currency = payment.currency or "INR"
            if opp is None:
                opp = self._find_or_create_opportunity(payment, action_type)
        else:
            amount = opp.amount_paise  # type: ignore[union-attr]
            currency = opp.currency or "INR"  # type: ignore[union-attr]

        action = RecoveryAction(
            opportunity_id=opp.id,  # type: ignore[union-attr]
            incident_id=self._incident_id,
            action_type=action_type,
            status=RecoveryStatus.PROPOSED,
            amount_paise=amount,
            currency=currency,
            confidence=conf,
            actor=AGENT_ACTOR,
            proposed_at=utcnow(),
            note=(str(note)[:1024] if note else None),
        )
        self._session.add(action)
        self._session.flush()

        ctx = self._build_context(
            action_type,
            amount_paise=amount,
            confidence=conf,
            payment=payment,
            opp=opp,
            current_action_id=action.id,
        )
        decision = self._engine.evaluate(ctx)

        record = self._session.scalars(
            sa.select(PolicyDecisionRecord)
            .where(PolicyDecisionRecord.action_id == action.id)
            .order_by(PolicyDecisionRecord.created_at.desc())
            .limit(1)
        ).first()
        if record is not None:
            action.policy_decision_id = record.id
        action.decided_at = decision.decided_at
        if decision.outcome.value == "BLOCKED":
            action.status = RecoveryStatus.REJECTED
        elif decision.outcome.value == "REQUIRES_APPROVAL":
            action.status = RecoveryStatus.PENDING_APPROVAL
        else:
            action.status = RecoveryStatus.POLICY_EVALUATED
        self._session.flush()

        audit.record(
            self._session,
            actor=AGENT_ACTOR,
            action="agent.action_requested",
            entity_type="recovery_action",
            entity_id=action.id,
            details={
                # Structured transition fields (docs/payment-invariants.md
                # invariant 12): this single row covers the action's creation
                # (from_status None) AND the gate's verdict, so the from/to
                # audit chain starts at this row instead of at the executor's
                # first transition.
                "from_status": None,
                "to_status": action.status.value,
                "incident_id": self._incident_id,
                "action_type": action_type.value,
                "amount_paise": amount,
                "currency": currency,
                "confidence": conf,
                "policy_outcome": decision.outcome.value,
                "rules_matched": decision.rules_matched,
                "note": note,
            },
        )

        evidence = [action.id, opp.id] + ([payment.id] if payment else [])  # type: ignore[union-attr]
        data = {
            "action_id": action.id,
            "opportunity_id": opp.id,  # type: ignore[union-attr]
            "action_type": action_type.value,
            "status": action.status.value,
            "amount_paise": amount,
            "currency": currency,
            "confidence": conf,
            "payment_id": payment.id if payment else None,
            "customer_id": ctx.customer_id,
            "policy": _decision_dict(decision),
            "executed": False,
        }
        name = (
            "request_payment_link"
            if action_type is ActionType.CREATE_PAYMENT_LINK
            else "request_recovery_execution"
        )
        log.info(
            "agent action requested",
            extra={
                "action_id": action.id,
                "action_type": action_type.value,
                "policy_outcome": decision.outcome.value,
                "incident_id": self._incident_id,
            },
        )
        return ToolResult(name, data, evidence)


__all__ = [
    "AGENT_ACTOR",
    "DERIVED_OPPORTUNITY_TYPE",
    "AgentTools",
    "ToolError",
    "ToolNotAllowed",
    "ToolResult",
]

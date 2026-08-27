"""Deterministic policy engine — the ONLY path by which a proposed financial
action may be authorized (ADR 0003).

Guarantees:
- TOTAL: `evaluate()` never raises. Malformed input, unknown action types,
  broken history sources, and internal errors all yield BLOCKED (fail closed).
- DETERMINISTIC: same (ActionContext, PolicyConfig, history state) always
  yields the same (outcome, reasons, rules_matched). No randomness, no LLM,
  no wall-clock influence on the outcome (timestamps only stamp the record).
- EXPLAINABLE: every rule that fired is listed in `rules_matched`; `reasons`
  carries the human-readable counterpart. Outcome precedence is
  BLOCKED > REQUIRES_APPROVAL > ALLOWED.
- AUDITED: with a session attached, every decision is persisted to
  `policy_decisions` (flushed, never committed — the caller owns the
  transaction) and every BLOCKED decision additionally lands in `audit_logs`.

ALLOWED (auto-execution) requires ALL of: action on the allowlist, no hard
block, no stopping rule, no rate limit, no duplicate, amount <= ceiling,
confidence >= floor, attempts < budget, and every stateful guard verifiable.
Anything short of that is at best REQUIRES_APPROVAL.
"""

from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db import utcnow
from app.logging import get_logger
from app.models import PolicyDecisionRecord
from app.ports import ActionContext, ActionType, PolicyDecision, PolicyOutcome
from app.services.policy import audit
from app.services.policy.config import PolicyConfig, failsafe_config, load_policy_config
from app.services.policy.history import PolicyHistory, SqlPolicyHistory

log = get_logger(__name__)

# Well-known ActionContext.metadata keys consumed by the engine.
META_STRATEGY_ID = "strategy_id"          # enables the per-strategy stopping rule
META_CURRENT_ACTION_ID = "current_action_id"  # excluded from history (no self-count)
META_IRREVERSIBLE = "irreversible_action"     # truthy -> hard block
META_REQUEST_ID = "request_id"                # propagated to the audit trail

# Non-financial actions: no money moves and no customer is contacted, so they
# skip approval thresholds, rate limits, and stopping rules — they ARE the
# escape hatch when automation halts. Still subject to malformed-input blocks,
# the allowlist, and (unless exempt) the kill switch.
SAFE_ACTIONS = frozenset({ActionType.NO_ACTION, ActionType.ESCALATE_HUMAN})

_BLOCK, _APPROVAL, _ALLOW = "block", "approval", "allow"


@dataclass(frozen=True)
class _Normalized:
    """Defensively normalized ActionContext. `errors` holds (rule, reason)
    pairs for every malformed field; any error means BLOCKED."""

    action_type: ActionType | None
    action_name: str
    amount_paise: int
    confidence: float
    actor: str
    currency: str
    incident_id: str | None
    opportunity_id: str | None
    customer_id: str | None
    customer_opted_out: bool
    attempts_so_far: int
    consecutive_failures: int
    metadata: dict[str, Any]
    errors: tuple[tuple[str, str], ...] = ()


def _nonneg_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _meta_str(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize(ctx: ActionContext) -> _Normalized:
    errors: list[tuple[str, str]] = []

    raw_type = getattr(ctx, "action_type", None)
    action_type: ActionType | None
    if isinstance(raw_type, ActionType):
        action_type = raw_type
        action_name = raw_type.value
    elif isinstance(raw_type, str):
        action_name = raw_type.strip().lower()
        try:
            action_type = ActionType(action_name)
        except ValueError:
            action_type = None
            errors.append(("malformed.action_type", f"unknown action type {raw_type!r}"))
    else:
        action_type = None
        action_name = repr(raw_type)
        errors.append(
            (
                "malformed.action_type",
                f"action type must be a string, got {type(raw_type).__name__}",
            )
        )

    raw_amount = getattr(ctx, "amount_paise", 0)
    amount = 0
    if isinstance(raw_amount, bool):
        amount = int(raw_amount)
    elif isinstance(raw_amount, int):
        amount = raw_amount
    elif isinstance(raw_amount, float) and math.isfinite(raw_amount) and raw_amount.is_integer():
        amount = int(raw_amount)
    elif isinstance(raw_amount, str) and raw_amount.strip().lstrip("+-").isdigit():
        amount = int(raw_amount.strip())
    else:
        errors.append(("malformed.amount", f"amount_paise {raw_amount!r} is not an integer"))
    if amount < 0:
        errors.append(("malformed.amount", f"amount_paise {amount} is negative"))

    raw_conf = getattr(ctx, "confidence", 0.0)
    try:
        confidence = float(raw_conf)
    except (TypeError, ValueError):
        confidence = 0.0
        errors.append(("malformed.confidence", f"confidence {raw_conf!r} is not a number"))
    if not (0.0 <= confidence <= 1.0):  # also rejects NaN (all comparisons False)
        errors.append(("malformed.confidence", f"confidence {raw_conf!r} is outside [0, 1]"))
        confidence = 0.0

    currency = (str(getattr(ctx, "currency", "") or "")).strip().upper()
    if currency != "INR":
        errors.append(
            ("malformed.currency", f"unsupported currency {currency!r}; policy thresholds are INR")
        )

    raw_meta = getattr(ctx, "metadata", None)
    metadata = dict(raw_meta) if isinstance(raw_meta, dict) else {}

    return _Normalized(
        action_type=action_type,
        action_name=action_name,
        amount_paise=amount,
        confidence=confidence,
        actor=str(getattr(ctx, "actor", "") or "unknown"),
        currency=currency or "INR",
        incident_id=_meta_str({"v": getattr(ctx, "incident_id", None)}, "v"),
        opportunity_id=_meta_str({"v": getattr(ctx, "opportunity_id", None)}, "v"),
        customer_id=_meta_str({"v": getattr(ctx, "customer_id", None)}, "v"),
        customer_opted_out=bool(getattr(ctx, "customer_opted_out", False)),
        attempts_so_far=_nonneg_int(getattr(ctx, "attempts_so_far", 0)),
        consecutive_failures=_nonneg_int(getattr(ctx, "consecutive_failures", 0)),
        metadata=metadata,
        errors=tuple(errors),
    )


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


def _context_dict(ctx: Any) -> dict[str, Any]:
    try:
        data = dataclasses.asdict(ctx) if dataclasses.is_dataclass(ctx) else {"raw": repr(ctx)}
    except (TypeError, ValueError):
        data = {"raw": repr(ctx)}
    return _jsonable(data)


class PolicyEngine:
    """YAML-driven deterministic gate. Satisfies `ports.PolicyEngineProto`."""

    def __init__(
        self,
        config: PolicyConfig,
        *,
        session: Session | None = None,
        history: PolicyHistory | None = None,
    ) -> None:
        """`session` enables decision persistence and (unless `history`
        overrides it) the stateful guards. Without a session the engine still
        evaluates every context-only rule, but auto-execution is impossible:
        the best outcome is REQUIRES_APPROVAL (stateful guards unverified)."""
        self._config = config
        self._session = session
        if history is not None:
            self._history: PolicyHistory | None = history
        elif session is not None:
            self._history = SqlPolicyHistory(session)
        else:
            self._history = None

    @classmethod
    def from_file(
        cls,
        path: str | None = None,
        *,
        session: Session | None = None,
        history: PolicyHistory | None = None,
    ) -> PolicyEngine:
        """Build an engine from the policy file (default: settings.POLICY_FILE).
        Raises PolicyConfigError if the file is broken — fail closed."""
        return cls(load_policy_config(path), session=session, history=history)

    @classmethod
    def failsafe(cls, reason: str = "policy configuration unavailable", *, session: Session | None = None) -> PolicyEngine:
        """An engine that BLOCKs everything. Use when the policy file cannot
        be loaded but an engine instance is still required."""
        return cls(failsafe_config(reason), session=session)

    @property
    def config(self) -> PolicyConfig:
        return self._config

    @property
    def policy_version(self) -> str:
        return self._config.policy_version

    # -- public API --------------------------------------------------------------

    def evaluate(self, ctx: ActionContext) -> PolicyDecision:
        """Total, deterministic gate. Never raises; always returns a decision."""
        norm: _Normalized | None = None
        try:
            norm = _normalize(ctx)
            decision = self._evaluate(norm)
        except Exception as exc:  # totality: any internal failure -> BLOCKED
            log.exception("policy evaluation failed closed", extra={"error": str(exc)})
            decision = PolicyDecision(
                outcome=PolicyOutcome.BLOCKED,
                reasons=[f"internal policy error; failing closed ({type(exc).__name__})"],
                rules_matched=["internal_error"],
                policy_version=self._config.policy_version,
            )
        if self._session is not None:
            try:
                self._persist(ctx, norm, decision)
            except Exception:
                # The decision still stands; persistence problems must not turn
                # a BLOCK into silence or an ALLOW into a crash upstream.
                log.exception("policy decision persistence failed")
        if decision.outcome is PolicyOutcome.BLOCKED:
            log.warning(
                "policy BLOCKED action",
                extra={
                    "action_type": norm.action_name if norm else "unknown",
                    "rules_matched": decision.rules_matched,
                    "policy_version": decision.policy_version,
                },
            )
        return decision

    # -- rule pipeline -------------------------------------------------------------

    def _evaluate(self, norm: _Normalized) -> PolicyDecision:
        cfg = self._config
        rules: list[str] = []
        reasons: list[str] = []
        blocked = False
        needs_approval = False

        def hit(rule: str, reason: str, severity: str) -> None:
            nonlocal blocked, needs_approval
            rules.append(rule)
            reasons.append(reason)
            if severity == _BLOCK:
                blocked = True
            elif severity == _APPROVAL:
                needs_approval = True

        # R00 malformed input — fail closed on anything we cannot interpret.
        for rule, reason in norm.errors:
            hit(rule, reason, _BLOCK)

        # R01 global kill switch.
        if cfg.kill_switch.enabled and norm.action_name not in cfg.kill_switch.exempt_actions:
            detail = cfg.kill_switch.reason or "no reason given"
            hit("kill_switch", f"global kill switch enabled ({detail})", _BLOCK)

        # R02 closed allowlist (refund is deliberately absent: no execution path).
        if norm.action_type is not None and norm.action_name not in cfg.allowlist:
            hit("allowlist", f"action {norm.action_name!r} is not on the policy allowlist", _BLOCK)

        # Safe actions carry no execution risk: if nothing blocked them, allow
        # immediately (they are the escape hatch, never the hazard).
        if norm.action_type in SAFE_ACTIONS and not blocked:
            hit("safe_action", f"{norm.action_name} moves no money and contacts no customer", _ALLOW)
            return PolicyDecision(
                outcome=PolicyOutcome.ALLOWED,
                reasons=reasons,
                rules_matched=rules,
                policy_version=cfg.policy_version,
            )

        # R03 hard blocks — no approval path exists for these.
        never = set(cfg.never_auto_execute)
        if norm.action_name in never:
            hit(
                f"never_auto_execute.{norm.action_name}",
                f"{norm.action_name} can never be authorized by the policy gate",
                _BLOCK,
            )
        if "irreversible_action" in never and norm.metadata.get(META_IRREVERSIBLE):
            hit(
                "never_auto_execute.irreversible_action",
                "action is flagged irreversible",
                _BLOCK,
            )
        if "customer_opted_out" in never and norm.customer_opted_out:
            hit(
                "never_auto_execute.customer_opted_out",
                "customer has opted out; no automated recovery contact is permitted",
                _BLOCK,
            )

        # Stateful guards (stopping rules, rate limits, duplicate protection).
        stateful_unverified = False
        if self._history is not None:
            self._stateful_guards(norm, hit)
        else:
            stateful_unverified = True

        # Auto-execute criteria — ALL must hold for ALLOWED. The effective
        # bounds are the stricter of auto_execute and require_human_approval,
        # so a config slip can only tighten the gate, never loosen it.
        ceiling = cfg.auto_amount_ceiling_paise
        floor = cfg.auto_confidence_floor
        if norm.amount_paise > ceiling:
            hit(
                "approval.amount",
                f"amount {norm.amount_paise} paise exceeds the auto-execute ceiling of "
                f"{ceiling} paise (INR {ceiling // 100})",
                _APPROVAL,
            )
        if norm.confidence < floor:
            hit(
                "approval.confidence",
                f"confidence {norm.confidence:.4f} is below the auto-execute floor of {floor}",
                _APPROVAL,
            )
        if norm.attempts_so_far >= cfg.auto_execute.max_attempts:
            hit(
                "approval.attempts",
                f"this is attempt {norm.attempts_so_far + 1}; the auto-execute budget is "
                f"{cfg.auto_execute.max_attempts}",
                _APPROVAL,
            )
        if stateful_unverified:
            hit(
                "stateful.unverified",
                "stateful guards (stopping rules, rate limits, duplicate protection) could not "
                "be verified without a history source; auto-execution is not permitted",
                _APPROVAL,
            )

        if not blocked and not needs_approval:
            hit(
                "auto_execute.ok",
                f"all auto-execute criteria met (amount <= {ceiling} paise, "
                f"confidence >= {floor}, attempts < {cfg.auto_execute.max_attempts}, "
                "no hard block, stopping rule, rate limit, or duplicate matched)",
                _ALLOW,
            )

        outcome = (
            PolicyOutcome.BLOCKED
            if blocked
            else PolicyOutcome.REQUIRES_APPROVAL
            if needs_approval
            else PolicyOutcome.ALLOWED
        )
        return PolicyDecision(
            outcome=outcome,
            reasons=reasons,
            rules_matched=rules,
            policy_version=cfg.policy_version,
        )

    def _stateful_guards(self, norm: _Normalized, hit) -> None:
        cfg = self._config
        history = self._history
        assert history is not None  # caller guarantees
        now = utcnow()
        exclude = _meta_str(norm.metadata, META_CURRENT_ACTION_ID)

        # R04 stopping rule per incident: the caller's signal and the recorded
        # history both count; the stricter (higher) reading wins.
        db_failures = (
            history.consecutive_failed_for_incident(norm.incident_id, exclude_action_id=exclude)
            if norm.incident_id
            else 0
        )
        failures = max(norm.consecutive_failures, db_failures)
        limit = cfg.stopping_rule.max_consecutive_failed_recoveries_per_incident
        if failures >= limit:
            hit(
                "stopping_rule.incident",
                f"{failures} consecutive failed recoveries on incident "
                f"{norm.incident_id or '<unlinked>'} (limit {limit}); automation is halted "
                "pending human review",
                _BLOCK,
            )

        # R05 stopping rule per strategy.
        strategy_id = _meta_str(norm.metadata, META_STRATEGY_ID)
        if strategy_id:
            s_failures = history.consecutive_failed_for_strategy(
                strategy_id, exclude_action_id=exclude
            )
            s_limit = cfg.stopping_rule.max_consecutive_failed_recoveries_per_strategy
            if s_failures >= s_limit:
                hit(
                    "stopping_rule.strategy",
                    f"{s_failures} consecutive failed actions for strategy {strategy_id} "
                    f"(limit {s_limit})",
                    _BLOCK,
                )

        # R06 per-incident action budget.
        if norm.incident_id:
            n_incident = history.count_actions_for_incident(norm.incident_id, exclude_action_id=exclude)
            if n_incident >= cfg.rate_limits.max_actions_per_incident:
                hit(
                    "rate_limit.incident",
                    f"incident {norm.incident_id} already has {n_incident} recovery actions "
                    f"(limit {cfg.rate_limits.max_actions_per_incident})",
                    _BLOCK,
                )

        if norm.customer_id:
            # R07 per-customer daily budget (UTC day).
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            n_customer = history.count_actions_for_customer_since(
                norm.customer_id, day_start, exclude_action_id=exclude
            )
            if n_customer >= cfg.rate_limits.max_actions_per_customer_per_day:
                hit(
                    "rate_limit.customer_daily",
                    f"customer {norm.customer_id} already has {n_customer} recovery actions "
                    f"today (limit {cfg.rate_limits.max_actions_per_customer_per_day})",
                    _BLOCK,
                )

            # R08 duplicate protection: same customer + action type inside the
            # cooldown window, unless the prior action conclusively ended.
            if norm.action_type is not None:
                last_at = history.last_active_action_at(
                    norm.customer_id, norm.action_type, exclude_action_id=exclude
                )
                if last_at is not None:
                    cooldown = timedelta(minutes=cfg.duplicate_protection.cooldown_minutes)
                    if now - last_at <= cooldown:
                        hit(
                            "duplicate.cooldown",
                            f"an active {norm.action_name} action for customer "
                            f"{norm.customer_id} already exists (since {last_at.isoformat()}, "
                            f"cooldown {cfg.duplicate_protection.cooldown_minutes} minutes)",
                            _BLOCK,
                        )

        # R09 global hourly budget.
        n_global = history.count_actions_global_since(now - timedelta(hours=1), exclude_action_id=exclude)
        if n_global >= cfg.rate_limits.max_actions_global_per_hour:
            hit(
                "rate_limit.global_hourly",
                f"{n_global} recovery actions in the last hour "
                f"(limit {cfg.rate_limits.max_actions_global_per_hour})",
                _BLOCK,
            )

    # -- persistence ------------------------------------------------------------

    def _persist(self, ctx: Any, norm: _Normalized | None, decision: PolicyDecision) -> None:
        record = PolicyDecisionRecord(
            action_id=_meta_str(norm.metadata, META_CURRENT_ACTION_ID) if norm else None,
            action_type=(norm.action_name if norm else "unknown")[:64],
            amount_paise=norm.amount_paise if norm else 0,
            currency=(norm.currency if norm else "INR")[:8],
            confidence=norm.confidence if norm else 0.0,
            outcome=decision.outcome,
            reasons=list(decision.reasons),
            rules_matched=list(decision.rules_matched),
            policy_version=decision.policy_version[:64],
            actor=(norm.actor if norm else "unknown")[:128],
            context=_context_dict(ctx),
            decided_at=decision.decided_at,
        )
        self._session.add(record)
        self._session.flush()
        # Blocked proposals are security-relevant: mirror them into the
        # append-only audit trail.
        if decision.outcome is PolicyOutcome.BLOCKED:
            audit.record(
                self._session,
                actor=record.actor,
                action="policy.action_blocked",
                entity_type="policy_decision",
                entity_id=record.id,
                details={
                    "action_type": record.action_type,
                    "outcome": decision.outcome.value,
                    "rules_matched": decision.rules_matched,
                    "reasons": decision.reasons,
                    "incident_id": norm.incident_id if norm else None,
                    "customer_id": norm.customer_id if norm else None,
                    "policy_version": record.policy_version,
                },
                request_id=_meta_str(norm.metadata, META_REQUEST_ID) if norm else None,
            )


__all__ = [
    "META_CURRENT_ACTION_ID",
    "META_IRREVERSIBLE",
    "META_REQUEST_ID",
    "META_STRATEGY_ID",
    "PolicyEngine",
    "SAFE_ACTIONS",
]

"""Ports: stable protocols, enums, and dataclasses shared across agents.

This module is the integration contract for the parallel feature agents:
- Gateway agent implements `PaymentGateway` (Razorpay test-mode via raw REST,
  plus a simulator implementation with identical behavior).
- Policy agent implements `PolicyEngineProto` (deterministic YAML-driven gate).
- Reasoner agent implements `ReasonerProto` (offline heuristic default; an LLM
  provider may be plugged in via env, but it NEVER executes financial actions).

Guiding principle: probabilistic AI proposes, deterministic policy decides,
payment infrastructure executes, verification proves.

Money convention: integer paise (INR) everywhere internally and in APIs.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, Sequence, runtime_checkable


# ---------------------------------------------------------------------------
# Shared enums (str-based so they serialize cleanly to JSON and DB varchar)
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    RETRY_PAYMENT = "retry_payment"
    CREATE_PAYMENT_LINK = "create_payment_link"
    NOTIFY_CUSTOMER = "notify_customer"
    EXTEND_GRACE_PERIOD = "extend_grace_period"
    PAUSE_SUBSCRIPTION = "pause_subscription"
    RESUME_SUBSCRIPTION = "resume_subscription"
    REFUND = "refund"
    ESCALATE_HUMAN = "escalate_human"
    NO_ACTION = "no_action"


class PolicyOutcome(str, Enum):
    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"


class IncidentStatus(str, Enum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSED = "DIAGNOSED"
    RECOVERING = "RECOVERING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    FALSE_POSITIVE = "FALSE_POSITIVE"


class RecoveryStatus(str, Enum):
    """State machine for recovery opportunities and recovery actions."""

    PROPOSED = "PROPOSED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"  # executed but outcome unverifiable (e.g. webhook lost)
    CANCELLED = "CANCELLED"
    ESCALATED = "ESCALATED"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Payment gateway port
# ---------------------------------------------------------------------------

@runtime_checkable
class PaymentGateway(Protocol):
    """Razorpay test-mode gateway (raw REST, no SDK) and its simulator twin.

    Methods return the raw gateway payload as plain dicts. Implementations must
    be idempotency-safe where noted (callers pass `idempotency_key`).
    """

    def create_order(
        self,
        *,
        amount_paise: int,
        currency: str = "INR",
        receipt: str | None = None,
        notes: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...

    def fetch_payment(self, payment_id: str) -> dict[str, Any]: ...

    def fetch_order(self, order_id: str) -> dict[str, Any]: ...

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        currency: str = "INR",
        customer: dict[str, Any] | None = None,
        description: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...

    def create_subscription(
        self,
        *,
        plan_id: str,
        customer_id: str | None = None,
        total_count: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool: ...


# ---------------------------------------------------------------------------
# Policy engine port — the ONLY path by which an action may be executed
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionContext:
    """Everything the deterministic policy engine needs to decide."""

    action_type: ActionType
    amount_paise: int
    confidence: float
    actor: str  # e.g. "agent:strategist", "human:ops@x.com", "system"
    currency: str = "INR"
    incident_id: str | None = None
    opportunity_id: str | None = None
    customer_id: str | None = None
    customer_opted_out: bool = False
    attempts_so_far: int = 0
    consecutive_failures: int = 0  # per incident; drives the stopping rule
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reasons: list[str]
    rules_matched: list[str] = field(default_factory=list)
    policy_version: str = "unknown"
    decided_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PolicyEngineProto(Protocol):
    """Deterministic gate. Every financial action passes through evaluate()
    before execution — no exceptions, including for LLM-suggested actions."""

    def evaluate(self, ctx: ActionContext) -> PolicyDecision: ...


# ---------------------------------------------------------------------------
# Reasoner port — probabilistic, advisory only, NEVER executes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceBundle:
    incident_id: str
    metric: str
    window_start: datetime | None = None
    window_end: datetime | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Hypothesis:
    title: str
    confidence: float
    supporting_evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InvestigationReport:
    incident_id: str
    summary: str
    hypotheses: list[Hypothesis] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    generated_by: str = "heuristic"  # "heuristic" | llm model name
    raw: dict[str, Any] = field(default_factory=dict)


class ReasonerProto(Protocol):
    """AI investigator. Produces hypotheses and recommendations only; its output
    feeds the policy gate like any other proposal."""

    def investigate(self, evidence: EvidenceBundle) -> InvestigationReport: ...


# ---------------------------------------------------------------------------
# Recovery strategy candidates (produced by the strategy generator, filtered
# and ranked, then each is gated through PolicyEngineProto before execution)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyCandidate:
    action_type: ActionType
    expected_recovery_paise: int
    confidence: float  # 0..1
    risk: str  # "low" | "medium" | "high"
    eligibility: bool  # hard eligibility (consent, attempt budget, windows)
    reason: str
    constraints: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "ActionType",
    "PolicyOutcome",
    "IncidentStatus",
    "RecoveryStatus",
    "Severity",
    "PaymentGateway",
    "ActionContext",
    "PolicyDecision",
    "PolicyEngineProto",
    "EvidenceBundle",
    "Hypothesis",
    "InvestigationReport",
    "ReasonerProto",
    "StrategyCandidate",
]

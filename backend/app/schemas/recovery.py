"""Recovery schemas — opportunities, plans (strategy candidates), and the
approve/reject/escalate/execute/cancel action contracts."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.ports import ActionType, PolicyOutcome, RecoveryStatus
from app.schemas.common import Paginated


class OpportunitySummary(BaseModel):
    id: str
    incident_id: str | None = None
    payment_id: str | None = None
    customer_id: str | None = None
    subscription_id: str | None = None
    opportunity_type: str
    status: RecoveryStatus
    amount_paise: int
    currency: str = "INR"
    expected_recovery_paise: int = 0
    confidence: float = 0.0
    risk: str = "low"
    reason: str | None = None
    created_at: datetime
    expires_at: datetime | None = None


class OpportunityListResponse(Paginated[OpportunitySummary]):
    pass


class OpportunityDetail(OpportunitySummary):
    constraints: dict[str, Any] = Field(default_factory=dict)
    actions: list["RecoveryActionView"] = Field(default_factory=list)


class StrategyOption(BaseModel):
    id: str
    action_type: ActionType
    rank: int = 0
    expected_recovery_paise: int = 0
    confidence: float = 0.0
    risk: str = "low"
    eligibility: bool = True
    reason: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    generated_by: str = "heuristic"
    selected: bool = False


class PolicyPreview(BaseModel):
    outcome: PolicyOutcome
    reasons: list[str] = Field(default_factory=list)


class RecoveryPlan(BaseModel):
    opportunity_id: str
    strategies: list[StrategyOption] = Field(default_factory=list)
    recommended_strategy_id: str | None = None
    policy_preview: PolicyPreview | None = None


class PolicyDecisionView(BaseModel):
    id: str
    outcome: PolicyOutcome
    reasons: list[str] = Field(default_factory=list)
    rules_matched: list[str] = Field(default_factory=list)
    policy_version: str = "unknown"
    decided_at: datetime


class RecoveryActionView(BaseModel):
    id: str
    opportunity_id: str
    strategy_id: str | None = None
    action_type: ActionType
    status: RecoveryStatus
    amount_paise: int
    currency: str = "INR"
    confidence: float = 0.0
    actor: str
    attempts: int = 0
    gateway_request_id: str | None = None
    policy_decision: PolicyDecisionView | None = None
    proposed_at: datetime
    executed_at: datetime | None = None
    verified_at: datetime | None = None
    last_error: str | None = None


# --- mutating requests ------------------------------------------------------

class ApproveRequest(BaseModel):
    actor: str = "human:unknown"
    note: str | None = None


class RejectRequest(BaseModel):
    actor: str = "human:unknown"
    reason: str


class EscalateRequest(BaseModel):
    actor: str = "human:unknown"
    reason: str


class ExecuteRequest(BaseModel):
    strategy_id: str | None = None  # defaults to the recommended strategy
    actor: str = "human:unknown"


class CancelRequest(BaseModel):
    actor: str = "human:unknown"
    reason: str | None = None


class ActionResponse(BaseModel):
    action_id: str | None = None
    opportunity_id: str
    status: RecoveryStatus
    message: str = ""
    policy_decision: PolicyDecisionView | None = None

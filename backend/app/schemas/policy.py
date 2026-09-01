"""Policy schemas — backtesting: read-only replay of historical policy
decisions against the CURRENT policy document (docs/policy.md §7)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.ports import PolicyOutcome


class PolicyBacktestRequest(BaseModel):
    """Filters for one backtest replay. All optional; an empty body replays
    every stored policy decision (up to ``limit``)."""

    environment: Literal["real_test", "research"] | None = None
    # Scope to decisions whose linked recovery action carries this
    # environment. None replays across BOTH environments (a read-only report
    # never mixes writes). Decisions with no linked recovery action carry no
    # provable environment and are only included when this is None.
    since: datetime | None = None  # only decisions with decided_at >= since
    until: datetime | None = None  # only decisions with decided_at <= until (inclusive)
    limit: int = Field(default=500, ge=1, le=5000)
    # Cap on decisions replayed (oldest first, deterministic truncation).


class PolicyBacktestFlip(BaseModel):
    """One historical decision whose outcome would change under the current
    policy document."""

    decision_id: str
    action_id: str | None = None
    action_type: str
    amount_paise: int
    currency: str = "INR"
    actor: str
    decided_at: datetime
    original_outcome: PolicyOutcome
    replayed_outcome: PolicyOutcome
    original_rules: list[str] = Field(default_factory=list)
    replayed_rules: list[str] = Field(default_factory=list)
    original_policy_version: str


class PolicyTransitionImpact(BaseModel):
    """Aggregate paise impact of one outcome transition, e.g. every decision
    that flips ALLOWED -> REQUIRES_APPROVAL."""

    from_outcome: PolicyOutcome
    to_outcome: PolicyOutcome
    count: int = 0
    amount_paise: int = 0


class PolicyBacktestResponse(BaseModel):
    run_id: str
    status: str  # completed
    started_at: datetime
    finished_at: datetime | None = None
    policy_version: str  # the CURRENT policy every decision was replayed against
    environment: str | None = None  # echo of the filter; None = both environments
    since: datetime | None = None
    until: datetime | None = None
    decisions_scanned: int = 0
    # Outcome tallies always carry all three keys (zero-filled).
    outcomes_original: dict[str, int] = Field(default_factory=dict)
    outcomes_replayed: dict[str, int] = Field(default_factory=dict)
    original_policy_versions: dict[str, int] = Field(default_factory=dict)
    unchanged_count: int = 0
    flip_count: int = 0
    flips: list[PolicyBacktestFlip] = Field(default_factory=list)
    transitions: list[PolicyTransitionImpact] = Field(default_factory=list)
    # Per-rule hit counts: replayed (current policy) vs as originally recorded.
    rule_hits: dict[str, int] = Field(default_factory=dict)
    rule_hits_original: dict[str, int] = Field(default_factory=dict)
    detail: str | None = None

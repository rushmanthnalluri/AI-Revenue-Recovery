"""Policy backtesting — replay historical policy decisions against the CURRENT
policy document (docs/product-strategy.md P2 "Lithic pattern").

Read-only analysis. For every stored ``policy_decisions`` row (optionally
window/environment-filtered) the original ``ActionContext`` is reconstructed
and re-evaluated against the current policy config, and the report answers:

- how many decisions would still be ALLOWED / BLOCKED / REQUIRES_APPROVAL;
- which decisions would FLIP (replayed outcome != recorded outcome);
- per-rule hit counts under the current policy (vs. as originally recorded);
- the paise impact of every outcome transition (e.g. ALLOWED -> BLOCKED).

Replay semantics (deterministic by construction):

- The replay engine runs WITHOUT a session, so it persists nothing — the
  backtest never writes ``policy_decisions`` rows.
- Stateful guards (stopping rules, rate limits, duplicate protection) are
  evaluated against an EMPTY history (:class:`_CleanHistory`): the report
  isolates the effect of the policy DOCUMENT itself. A decision originally
  blocked only by a stateful guard (e.g. ``rate_limit.customer_daily``) may
  replay as less strict; the flip's ``original_rules`` vs ``replayed_rules``
  make that visible. With an empty history no outcome depends on the wall
  clock, so replays are deterministic: same rows + same policy file -> same
  report.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import ids
from app.db import utcnow
from app.logging import get_logger
from app.models import PolicyDecisionRecord, RecoveryAction
from app.ports import ActionContext, PolicyOutcome
from app.schemas.policy import PolicyBacktestRequest
from app.services.policy.config import PolicyConfig, load_policy_config
from app.services.policy.engine import PolicyEngine

log = get_logger(__name__)

_CONTEXT_FIELDS = frozenset(f.name for f in dataclasses.fields(ActionContext))


class _CleanHistory:
    """PolicyHistory stub: every signal reads as a clean slate (zero counts,
    no streaks, no active duplicates). Structural, not a shortcut — with no
    history rows the wall clock cannot influence any guard outcome, which is
    what makes the replay deterministic."""

    def count_actions_for_incident(
        self, incident_id: str, *, exclude_action_id: str | None = None
    ) -> int:
        return 0

    def count_actions_for_customer_since(
        self, customer_id: str, since: datetime, *, exclude_action_id: str | None = None
    ) -> int:
        return 0

    def count_actions_global_since(
        self, since: datetime, *, exclude_action_id: str | None = None
    ) -> int:
        return 0

    def last_active_action_at(
        self,
        customer_id: str,
        action_type: Any,
        *,
        exclude_action_id: str | None = None,
    ) -> datetime | None:
        return None

    def consecutive_failed_for_incident(
        self, incident_id: str, *, exclude_action_id: str | None = None
    ) -> int:
        return 0

    def consecutive_failed_for_strategy(
        self, strategy_id: str, *, exclude_action_id: str | None = None
    ) -> int:
        return 0


@dataclass(frozen=True)
class BacktestFlip:
    """One decision whose outcome changes under the current policy."""

    decision_id: str
    action_id: str | None
    action_type: str
    amount_paise: int
    currency: str
    actor: str
    decided_at: datetime
    original_outcome: PolicyOutcome
    replayed_outcome: PolicyOutcome
    original_rules: list[str]
    replayed_rules: list[str]
    original_policy_version: str


@dataclass(frozen=True)
class TransitionImpact:
    """Aggregate paise impact of one outcome transition."""

    from_outcome: PolicyOutcome
    to_outcome: PolicyOutcome
    count: int
    amount_paise: int


@dataclass
class PolicyBacktestReport:
    run_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    policy_version: str
    environment: str | None
    since: datetime | None
    until: datetime | None
    decisions_scanned: int = 0
    outcomes_original: dict[str, int] = field(default_factory=dict)
    outcomes_replayed: dict[str, int] = field(default_factory=dict)
    original_policy_versions: dict[str, int] = field(default_factory=dict)
    unchanged_count: int = 0
    flip_count: int = 0
    flips: list[BacktestFlip] = field(default_factory=list)
    transitions: list[TransitionImpact] = field(default_factory=list)
    rule_hits: dict[str, int] = field(default_factory=dict)
    rule_hits_original: dict[str, int] = field(default_factory=dict)
    detail: str | None = None


def _decisions_stmt(req: PolicyBacktestRequest) -> sa.Select:
    """Stored decisions, oldest first (deterministic truncation at ``limit``).

    policy_decisions carries no environment column; the environment scope is
    derived through the soft action_id reference to recovery_actions (the
    decision's authorizing action). Under an environment filter, decisions
    with no matching action row are excluded — their provenance cannot be
    proven, and the two data environments never mix.
    """
    stmt = sa.select(PolicyDecisionRecord).outerjoin(
        RecoveryAction, PolicyDecisionRecord.action_id == RecoveryAction.id
    )
    if req.environment is not None:
        stmt = stmt.where(RecoveryAction.environment == req.environment)
    if req.since is not None:
        stmt = stmt.where(PolicyDecisionRecord.decided_at >= req.since)
    if req.until is not None:
        stmt = stmt.where(PolicyDecisionRecord.decided_at <= req.until)
    return stmt.order_by(
        PolicyDecisionRecord.decided_at.asc(), PolicyDecisionRecord.id.asc()
    ).limit(req.limit)


def _replay_context(record: PolicyDecisionRecord) -> ActionContext:
    """Rebuild the ActionContext the engine originally saw. The persisted
    ``context`` JSON holds the full pre-normalization context; the record's
    top-level columns (the normalized values) fill anything missing, so even
    sparse/legacy rows replay instead of crashing the report."""
    data: dict[str, Any] = {}
    if isinstance(record.context, dict):
        data = {k: v for k, v in record.context.items() if k in _CONTEXT_FIELDS}
    data.setdefault("action_type", record.action_type)
    data.setdefault("amount_paise", record.amount_paise)
    data.setdefault("confidence", record.confidence)
    data.setdefault("actor", record.actor)
    try:
        return ActionContext(**data)
    except TypeError:
        return ActionContext(
            action_type=record.action_type,
            amount_paise=record.amount_paise,
            confidence=record.confidence,
            actor=record.actor,
            currency=record.currency,
        )


def _outcome_dict(counter: Counter[str]) -> dict[str, int]:
    # Zero-fill every outcome so consumers never have to handle missing keys.
    return {outcome.value: int(counter.get(outcome.value, 0)) for outcome in PolicyOutcome}


def run_policy_backtest(
    db: Session,
    req: PolicyBacktestRequest | None = None,
    *,
    config: PolicyConfig | None = None,
) -> PolicyBacktestReport:
    """Replay stored policy decisions against the current policy. READ-ONLY:
    the replay engine has no session, so nothing is persisted or committed."""
    req = req or PolicyBacktestRequest()
    config = config if config is not None else load_policy_config()
    engine = PolicyEngine(config, history=_CleanHistory())  # session=None: no persistence
    started_at = utcnow()

    report = PolicyBacktestReport(
        run_id=ids.new_id("pbt_"),
        status="completed",
        started_at=started_at,
        finished_at=None,
        policy_version=config.policy_version,
        environment=req.environment,
        since=req.since,
        until=req.until,
    )

    original_outcomes: Counter[str] = Counter()
    replayed_outcomes: Counter[str] = Counter()
    versions: Counter[str] = Counter()
    rule_hits: Counter[str] = Counter()
    rule_hits_original: Counter[str] = Counter()
    # (from, to) -> [count, paise]
    transitions: dict[tuple[PolicyOutcome, PolicyOutcome], list[int]] = {}

    records = db.scalars(_decisions_stmt(req)).all()
    for record in records:
        original = record.outcome
        original_outcomes[original.value] += 1
        versions[str(record.policy_version)] += 1
        rule_hits_original.update(record.rules_matched or [])

        decision = engine.evaluate(_replay_context(record))
        replayed = decision.outcome
        replayed_outcomes[replayed.value] += 1
        rule_hits.update(decision.rules_matched)

        if replayed is not original:
            report.flips.append(
                BacktestFlip(
                    decision_id=record.id,
                    action_id=record.action_id,
                    action_type=record.action_type,
                    amount_paise=int(record.amount_paise or 0),
                    currency=record.currency,
                    actor=record.actor,
                    decided_at=record.decided_at,
                    original_outcome=original,
                    replayed_outcome=replayed,
                    original_rules=list(record.rules_matched or []),
                    replayed_rules=list(decision.rules_matched),
                    original_policy_version=record.policy_version,
                )
            )
            bucket = transitions.setdefault((original, replayed), [0, 0])
            bucket[0] += 1
            bucket[1] += int(record.amount_paise or 0)

    report.decisions_scanned = len(records)
    report.outcomes_original = _outcome_dict(original_outcomes)
    report.outcomes_replayed = _outcome_dict(replayed_outcomes)
    report.original_policy_versions = dict(sorted(versions.items()))
    report.flip_count = len(report.flips)
    report.unchanged_count = report.decisions_scanned - report.flip_count
    report.transitions = [
        TransitionImpact(
            from_outcome=src,
            to_outcome=dst,
            count=count,
            amount_paise=paise,
        )
        for (src, dst), (count, paise) in sorted(
            transitions.items(), key=lambda item: (item[0][0].value, item[0][1].value)
        )
    ]
    report.rule_hits = dict(sorted(rule_hits.items()))
    report.rule_hits_original = dict(sorted(rule_hits_original.items()))
    report.finished_at = utcnow()
    if report.decisions_scanned:
        report.detail = (
            f"replayed {report.decisions_scanned} policy decision(s) against "
            f"{config.policy_version}: {report.flip_count} would flip"
        )
    else:
        report.detail = "no policy decisions matched the filters"
    log.info(
        "policy backtest completed",
        extra={
            "run_id": report.run_id,
            "decisions_scanned": report.decisions_scanned,
            "flip_count": report.flip_count,
            "policy_version": config.policy_version,
            "environment": req.environment,
        },
    )
    return report


__all__ = [
    "BacktestFlip",
    "PolicyBacktestReport",
    "TransitionImpact",
    "run_policy_backtest",
]

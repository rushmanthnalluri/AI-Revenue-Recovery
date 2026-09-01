"""Policy endpoints — read-only analysis over the deterministic gate.

POST /backtest replays historical policy decisions against the CURRENT policy
file (docs/policy.md §7). The replay itself writes nothing; following the
detection/evaluation run convention, the run joins the audit trail (one
audit_logs row, committed by this layer).
"""

import dataclasses

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.policy import (
    PolicyBacktestFlip,
    PolicyBacktestRequest,
    PolicyBacktestResponse,
    PolicyTransitionImpact,
)
from app.services.policy import audit
from app.services.policy.backtest import run_policy_backtest
from app.services.policy.config import PolicyConfigError, load_policy_config

router = APIRouter(prefix="/api/v1/policy", tags=["policy"])


@router.post("/backtest", response_model=PolicyBacktestResponse)
def backtest_policy(
    body: PolicyBacktestRequest | None = None,
    db: Session = Depends(get_db),
) -> PolicyBacktestResponse:
    """Replay stored policy decisions against the CURRENT policy document.

    Reports how many historical decisions would still be ALLOWED / BLOCKED /
    REQUIRES_APPROVAL, which would flip, per-rule hit counts, and the paise
    impact of every outcome transition. Read-only: the replay engine runs
    without a session (no new policy_decisions rows); the only write is the
    audit row recording the run itself (same convention as detection.run).
    """
    req = body or PolicyBacktestRequest()
    try:
        config = load_policy_config()
    except PolicyConfigError as exc:
        # A broken policy file fails the whole gate closed at startup; here it
        # surfaces as 503 rather than a guessed-at report.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    report = run_policy_backtest(db, req, config=config)
    # The replay persisted nothing, but the run itself joins the audit trail,
    # stamped with the environment it scoped to ("all" = unfiltered report).
    entry = audit.record(
        db,
        actor="system:policy_backtest",
        action="policy.backtest",
        entity_type="policy_backtest_run",
        entity_id=report.run_id,
        details={
            "environment": req.environment,
            "since": req.since.isoformat() if req.since else None,
            "until": req.until.isoformat() if req.until else None,
            "limit": req.limit,
            "policy_version": report.policy_version,
            "decisions_scanned": report.decisions_scanned,
            "flip_count": report.flip_count,
            "outcomes_replayed": report.outcomes_replayed,
        },
    )
    entry.environment = req.environment or "all"
    db.commit()
    return PolicyBacktestResponse(
        run_id=report.run_id,
        status=report.status,
        started_at=report.started_at,
        finished_at=report.finished_at,
        policy_version=report.policy_version,
        environment=report.environment,
        since=report.since,
        until=report.until,
        decisions_scanned=report.decisions_scanned,
        outcomes_original=report.outcomes_original,
        outcomes_replayed=report.outcomes_replayed,
        original_policy_versions=report.original_policy_versions,
        unchanged_count=report.unchanged_count,
        flip_count=report.flip_count,
        flips=[PolicyBacktestFlip(**dataclasses.asdict(f)) for f in report.flips],
        transitions=[
            PolicyTransitionImpact(**dataclasses.asdict(t)) for t in report.transitions
        ],
        rule_hits=report.rule_hits,
        rule_hits_original=report.rule_hits_original,
        detail=report.detail,
    )

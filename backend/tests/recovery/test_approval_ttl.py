"""Approval TTL lapse-on-read (policy rule `approval.pending_approval_ttl_hours`).

A PENDING_APPROVAL action holds the opportunity's execution slot (and the
policy gate's duplicate-protection cooldown) while it waits for a human. When
the policy document configures `approval.pending_approval_ttl_hours`, an
action whose wait EXCEEDS the TTL lapses back to PROPOSED the next time the
executor loads it (execute/approve/reject/escalate/cancel), with a policy
decision record and an audited transition stamped actor system:approval_ttl.

The shipped policies/default.yaml sets no TTL, so the default behavior is
unchanged: approvals wait for an explicit approve/reject, indefinitely.
"""

from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml

import app.models as models
from app.db import utcnow
from app.ports import PolicyOutcome, RecoveryStatus
from app.services.policy import PolicyEngine
from app.services.recovery import RecoveryExecutor
from app.services.recovery.executor import (
    APPROVAL_TTL_ACTOR,
    APPROVAL_TTL_RULE,
    InvalidStateError,
)

_REAL_POLICY = Path(__file__).resolve().parents[3] / "policies" / "default.yaml"


def _ttl_engine(db_session, tmp_path, hours: int) -> PolicyEngine:
    """The real policy file plus an approval TTL — the full loader path."""
    data = yaml.safe_load(_REAL_POLICY.read_bytes())
    data["approval"] = {"pending_approval_ttl_hours": hours}
    variant = tmp_path / "policy_ttl.yaml"
    variant.write_text(yaml.safe_dump(data), encoding="utf-8")
    return PolicyEngine.from_file(str(variant), session=db_session)


def _pending_action(db_session, make_opportunity, make_proposed_action, *, age: timedelta):
    """One PENDING_APPROVAL action parked `age` ago (decided_at is what the
    TTL measures the approval wait from)."""
    opp = make_opportunity()
    action = make_proposed_action(
        opp,
        status=RecoveryStatus.PENDING_APPROVAL,
        confidence=0.5,
        proposed_at=utcnow() - age,
        decided_at=utcnow() - age,
    )
    return opp, action


def _audit_rows(db_session, action_id: str) -> list[models.AuditLog]:
    return list(
        db_session.scalars(
            sa.select(models.AuditLog)
            .where(
                models.AuditLog.entity_type == "recovery_action",
                models.AuditLog.entity_id == action_id,
            )
            .order_by(models.AuditLog.created_at, models.AuditLog.id)
        )
    )


class TestTtlDisabledByDefault:
    def test_stale_approval_never_lapses_without_the_rule(
        self, db_session, make_executor, sim_gateway, make_opportunity, make_proposed_action
    ):
        # The default policy file has no approval section: a 72h-old
        # PENDING_APPROVAL action is still approvable (today's behavior).
        opp, action = _pending_action(
            db_session, make_opportunity, make_proposed_action, age=timedelta(hours=72)
        )
        executor = make_executor(sim_gateway)

        approved = executor.approve(opp.id, actor="human:ops")

        assert approved.status is RecoveryStatus.APPROVED
        assert approved.approved_by == "human:ops"
        assert not any(r.actor == APPROVAL_TTL_ACTOR for r in _audit_rows(db_session, action.id))

    def test_stale_approval_still_refuses_execute_without_the_rule(
        self, db_session, make_executor, sim_gateway, make_opportunity, make_proposed_action
    ):
        opp, _action = _pending_action(
            db_session, make_opportunity, make_proposed_action, age=timedelta(hours=72)
        )
        with pytest.raises(InvalidStateError, match="awaits human approval"):
            make_executor(sim_gateway).execute(opp.id, actor="agent:strategist")


class TestLapseOnRead:
    def test_wait_beyond_ttl_lapses_to_proposed(
        self, db_session, tmp_path, sim_gateway, make_opportunity, make_proposed_action
    ):
        engine = _ttl_engine(db_session, tmp_path, hours=1)
        opp, action = _pending_action(
            db_session, make_opportunity, make_proposed_action, age=timedelta(hours=2)
        )
        executor = RecoveryExecutor(db_session, sim_gateway, policy_engine=engine)

        with pytest.raises(InvalidStateError, match="no action awaiting approval"):
            executor.approve(opp.id, actor="human:ops")

        db_session.refresh(action)
        db_session.refresh(opp)
        assert action.status is RecoveryStatus.PROPOSED
        assert opp.status is RecoveryStatus.PROPOSED  # the rollup shadows the action

        (lapse,) = [r for r in _audit_rows(db_session, action.id) if r.actor == APPROVAL_TTL_ACTOR]
        assert lapse.action == "recovery.action.proposed"
        assert lapse.details["from_status"] == "PENDING_APPROVAL"
        assert lapse.details["to_status"] == "PROPOSED"
        assert APPROVAL_TTL_RULE in lapse.details["rules_matched"]

        record = db_session.scalar(
            sa.select(models.PolicyDecisionRecord).where(
                models.PolicyDecisionRecord.action_id == action.id,
                models.PolicyDecisionRecord.actor == APPROVAL_TTL_ACTOR,
            )
        )
        assert record is not None
        assert record.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert record.rules_matched == [APPROVAL_TTL_RULE]
        assert record.policy_version == engine.policy_version

    def test_lapse_also_fires_on_execute(
        self, db_session, tmp_path, sim_gateway, make_opportunity, make_proposed_action
    ):
        engine = _ttl_engine(db_session, tmp_path, hours=1)
        opp, action = _pending_action(
            db_session, make_opportunity, make_proposed_action, age=timedelta(hours=2)
        )
        executor = RecoveryExecutor(db_session, sim_gateway, policy_engine=engine)

        # The stale approval lapses and the action is re-gated from PROPOSED;
        # confidence 0.5 is under the floor, so it parks again — a fresh wait.
        action.confidence = 0.5
        db_session.commit()
        result = executor.execute(opp.id, actor="agent:strategist")

        assert result.status is RecoveryStatus.PENDING_APPROVAL
        assert result.decided_at > utcnow() - timedelta(minutes=1)  # fresh decision
        record = db_session.get(models.PolicyDecisionRecord, result.policy_decision_id)
        assert record.actor == "agent:strategist"  # a real gate decision, not the lapse
        assert len(sim_gateway.payment_links) == 0

    def test_lapsed_action_can_still_recover_after_reapproval(
        self, db_session, tmp_path, sim_gateway, make_opportunity, make_proposed_action
    ):
        engine = _ttl_engine(db_session, tmp_path, hours=1)
        opp, action = _pending_action(
            db_session, make_opportunity, make_proposed_action, age=timedelta(hours=2)
        )
        action.confidence = 0.95  # auto-execute band once re-gated
        db_session.commit()
        executor = RecoveryExecutor(db_session, sim_gateway, policy_engine=engine)

        result = executor.execute(opp.id, actor="agent:strategist")

        assert result.id == action.id
        assert result.status is RecoveryStatus.RECOVERED  # sim pays links inline
        assert len(sim_gateway.payment_links) == 1


class TestBoundary:
    def test_just_under_the_ttl_stays_approvable(
        self, db_session, tmp_path, sim_gateway, make_opportunity, make_proposed_action
    ):
        engine = _ttl_engine(db_session, tmp_path, hours=1)
        opp, action = _pending_action(
            db_session,
            make_opportunity,
            make_proposed_action,
            age=timedelta(hours=1) - timedelta(minutes=1),
        )
        executor = RecoveryExecutor(db_session, sim_gateway, policy_engine=engine)

        approved = executor.approve(opp.id, actor="human:ops")
        assert approved.status is RecoveryStatus.APPROVED
        assert not any(
            r.actor == APPROVAL_TTL_ACTOR for r in _audit_rows(db_session, action.id)
        )

    def test_just_over_the_ttl_lapses(
        self, db_session, tmp_path, sim_gateway, make_opportunity, make_proposed_action
    ):
        engine = _ttl_engine(db_session, tmp_path, hours=1)
        opp, action = _pending_action(
            db_session,
            make_opportunity,
            make_proposed_action,
            age=timedelta(hours=1) + timedelta(seconds=5),
        )
        executor = RecoveryExecutor(db_session, sim_gateway, policy_engine=engine)

        with pytest.raises(InvalidStateError):
            executor.approve(opp.id, actor="human:ops")
        db_session.refresh(action)
        assert action.status is RecoveryStatus.PROPOSED

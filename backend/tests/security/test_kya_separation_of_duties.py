"""Separation of duties (KYA-lite): self-/same-cohort approval is flagged.

The policy gate gained an opt-in, outcome-neutral warning rule,
`separation_of_duties.self_approval` (engine R10): when a caller supplies the
proposer and approver principals and they match, the persisted decision
records the warning. The recovery approve endpoint re-gates the action with
both principals so every approval leaves that signal.

Honest boundary (docs/security-testing.md): under the single shared demo key
the proposer principal is unresolvable, so the API conservatively resolves an
unattributed proposer to the approver's own principal — the check fails
toward a warning, never toward silence, and never blocks the approval.
"""

import sqlalchemy as sa

import app.models as models
from app.db import utcnow
from app.ports import ActionContext, ActionType, PolicyOutcome, RecoveryStatus
from app.services.policy import PolicyEngine
from app.services.policy.engine import (
    META_APPROVER_PRINCIPAL,
    META_CURRENT_ACTION_ID,
    META_PROPOSER_PRINCIPAL,
)

API_KEY = {"X-API-Key": "dev-key"}
SOD_RULE = "separation_of_duties.self_approval"
APPROVAL_AMOUNT = 5_000_000  # above the ₹5,000 auto-execute ceiling


def _ctx(**metadata):
    return ActionContext(
        action_type=ActionType.RETRY_PAYMENT,
        amount_paise=100_000,
        confidence=0.95,
        actor="human:ops",
        currency="INR",
        metadata=metadata,
    )


class TestEngineSelfApprovalRule:
    """R10 is additive and default-preserving: it records, never decides."""

    def test_equal_principals_record_warning_without_changing_outcome(self, db_session):
        engine = PolicyEngine.from_file(session=db_session)
        with_signal = engine.evaluate(
            _ctx(**{META_PROPOSER_PRINCIPAL: "demo-operator", META_APPROVER_PRINCIPAL: "demo-operator"})
        )
        without_signal = engine.evaluate(_ctx())
        assert SOD_RULE in with_signal.rules_matched
        assert SOD_RULE not in without_signal.rules_matched
        # the warning is not enforcement: the outcome is untouched
        assert with_signal.outcome == without_signal.outcome

    def test_different_principals_record_no_warning(self, db_session):
        engine = PolicyEngine.from_file(session=db_session)
        decision = engine.evaluate(
            _ctx(**{META_PROPOSER_PRINCIPAL: "ops-lead", META_APPROVER_PRINCIPAL: "demo-operator"})
        )
        assert SOD_RULE not in decision.rules_matched

    def test_single_sided_metadata_records_no_warning(self, db_session):
        engine = PolicyEngine.from_file(session=db_session)
        decision = engine.evaluate(_ctx(**{META_APPROVER_PRINCIPAL: "demo-operator"}))
        assert SOD_RULE not in decision.rules_matched

    def test_warning_also_records_for_safe_actions(self, db_session):
        """Placement is before the safe-action early return: even an
        ESCALATE_HUMAN re-gate carries the warning when principals match."""
        engine = PolicyEngine.from_file(session=db_session)
        decision = engine.evaluate(
            ActionContext(
                action_type=ActionType.ESCALATE_HUMAN,
                amount_paise=0,
                confidence=1.0,
                actor="human:ops",
                metadata={
                    META_PROPOSER_PRINCIPAL: "demo-operator",
                    META_APPROVER_PRINCIPAL: "demo-operator",
                },
            )
        )
        assert decision.outcome is PolicyOutcome.ALLOWED
        assert SOD_RULE in decision.rules_matched


class TestApproveSeparationOfDuties:
    def _pending_approval(self, client, make_opportunity, make_payment, db_session):
        """Drive an opportunity into PENDING_APPROVAL via the API (proposer
        actor human:console under the shared demo key)."""
        opp = make_opportunity(amount_paise=APPROVAL_AMOUNT, payment=make_payment())
        db_session.commit()
        resp = client.post(
            f"/api/v1/recovery/{opp.id}/execute",
            json={"actor": "human:console"},
            headers=API_KEY,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "PENDING_APPROVAL"
        return opp

    def _sod_records(self, db_session, action_id):
        rows = db_session.scalars(
            sa.select(models.PolicyDecisionRecord).where(
                models.PolicyDecisionRecord.action_id == action_id
            )
        ).all()
        return [r for r in rows if SOD_RULE in (r.rules_matched or [])]

    def test_same_cohort_approval_records_warning(
        self, client, db_session, make_opportunity, make_payment
    ):
        """Propose and approve under the SAME shared key (different declared
        actors — the key cannot tell them apart): the gate records the
        self-approval warning, and the approval still succeeds."""
        opp = self._pending_approval(client, make_opportunity, make_payment, db_session)
        action = db_session.scalar(
            sa.select(models.RecoveryAction).where(
                models.RecoveryAction.opportunity_id == opp.id
            )
        )

        resp = client.post(
            f"/api/v1/recovery/{opp.id}/approve",
            json={"actor": "human:ops", "note": "reviewed"},
            headers=API_KEY,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "APPROVED"  # warning, never a block

        (record,) = self._sod_records(db_session, action.id)
        assert record.actor == "human:ops"  # approver's attributed actor
        assert record.outcome is not PolicyOutcome.BLOCKED
        assert any("separation of duties" in reason for reason in record.reasons)
        # proposer/approver principals are visible in the persisted context
        assert record.context["metadata"][META_PROPOSER_PRINCIPAL] == "demo-operator"
        assert record.context["metadata"][META_APPROVER_PRINCIPAL] == "demo-operator"

    def test_different_principals_approve_without_warning(
        self, client, db_session, make_opportunity
    ):
        """An action proposed by an ATTRIBUTED actor (@kya:ops-lead) approved
        under the demo cohort key: principals differ, no warning — but the
        re-gate decision is still persisted for the audit trail."""
        opp = make_opportunity(amount_paise=APPROVAL_AMOUNT)
        action = models.RecoveryAction(
            opportunity_id=opp.id,
            incident_id=opp.incident_id,
            action_type=ActionType.CREATE_PAYMENT_LINK,
            status=RecoveryStatus.PENDING_APPROVAL,
            amount_paise=APPROVAL_AMOUNT,
            confidence=0.9,
            actor="human:alice@kya:ops-lead",  # attributed proposer
            proposed_at=utcnow(),
            decided_at=utcnow(),
        )
        db_session.add(action)
        db_session.commit()

        resp = client.post(
            f"/api/v1/recovery/{opp.id}/approve",
            json={"actor": "human:ops"},
            headers=API_KEY,
        )
        assert resp.status_code == 200, resp.text
        assert self._sod_records(db_session, action.id) == []
        regate = db_session.scalars(
            sa.select(models.PolicyDecisionRecord).where(
                models.PolicyDecisionRecord.action_id == action.id,
                models.PolicyDecisionRecord.actor == "human:ops",
            )
        ).all()
        assert len(regate) == 1
        assert regate[0].context["metadata"][META_PROPOSER_PRINCIPAL] == "ops-lead"
        assert regate[0].context["metadata"][META_APPROVER_PRINCIPAL] == "demo-operator"

    def test_refused_approval_records_nothing(self, client, db_session, make_opportunity):
        """A 409 approve (nothing awaiting approval) persists no re-gate
        decision and no principal binding."""
        opp = make_opportunity()
        db_session.commit()
        resp = client.post(
            f"/api/v1/recovery/{opp.id}/approve", json={"actor": "human:ops"}, headers=API_KEY
        )
        assert resp.status_code == 409
        assert db_session.scalar(
            sa.select(sa.func.count()).select_from(models.PolicyDecisionRecord)
        ) == 0
        assert db_session.scalar(
            sa.select(sa.func.count())
            .select_from(models.AuditLog)
            .where(models.AuditLog.action == "recovery.principal_bound")
        ) == 0

"""Invariant 12: EVERY financial state transition writes an audit row.

Property-style sweep across the recovery_actions transition table. One
property, applied to every reachable path through the state machine:

    The audit trail for an action contains a creation record
    (recovery.action.proposed or agent.action_requested — the only two real
    producers of action rows) followed by one row per status transition,
    forming a CONTIGUOUS chain — each row's from_status equals the previous
    row's to_status — whose final to_status equals the action's current
    status. Every row carries actor (+ request_id for actor-driven flows).
    No silent transitions; no orphan rows.

Flows that need an action pre-seeded in a specific state (cancel/escalate
from PROPOSED, in-flight refusal) create the row directly — a state no real
code path produces without an audit record — so those assert the transition
chain only, not the creation record.

Existing proof for the single happy path:
tests/recovery/test_executor.py::TestAuditTrail::test_every_transition_is_audited_with_actor_and_request_id
This module sweeps the whole table: auto-execute, approval lane, reject,
cancel, escalate, gateway 4xx, gateway timeout/UNKNOWN, resolve (both
outcomes), webhook-driven verification, agent-tool-created rows, in-flight
refusal (no new rows), and opportunity-level transitions.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

import app.models as models
from app.ports import ActionType, RecoveryStatus
from app.services.agent.tools import AgentTools
from app.services.razorpay.errors import GatewayTransientError
from app.services.recovery import InvalidStateError, RecoveryExecutor

from tests.security.conftest import ConfusedGateway, CountingGateway

ACTOR = "human:sweep"


# --- gateway doubles ----------------------------------------------------------


class TimeoutGateway(CountingGateway):
    """Every mutation dies in flight (ambiguous outcome -> UNKNOWN)."""

    def create_order(self, **kw):
        self.mutation_calls += 1
        raise GatewayTransientError("simulated timeout mid-mutation")

    def create_payment_link(self, **kw):
        self.mutation_calls += 1
        raise GatewayTransientError("simulated timeout mid-mutation")


class CapturedFetchGateway(CountingGateway):
    """fetch_payment answers 'captured' for the ASKED id (honest truth)."""

    def fetch_payment(self, payment_id: str):
        self.fetch_calls += 1
        return {
            "id": payment_id,
            "entity": "payment",
            "status": "captured",
            "captured": True,
            "amount": 100_000,
        }


# --- the property -------------------------------------------------------------


def _rows(db, entity_type, entity_id):
    return list(
        db.scalars(
            sa.select(models.AuditLog)
            .where(
                models.AuditLog.entity_type == entity_type,
                models.AuditLog.entity_id == entity_id,
            )
            .order_by(models.AuditLog.created_at, models.AuditLog.id)
        )
    )


def assert_transition_audit(
    db,
    action: models.RecoveryAction,
    *,
    final: RecoveryStatus,
    first_from: str | None = None,
    creation: str | None = "recovery.action.proposed",
    request_id: str | None = None,
) -> list:
    """The invariant-12 property over one action's audit trail. Returns the
    ordered list of audit rows for flow-specific extras.

    creation: the audit action that must appear as the creation record
    ("recovery.action.proposed" for executor-created rows,
    "agent.action_requested" for tool-created rows, None for test-seeded
    rows whose creation bypassed every real path).
    first_from: expected from_status of the first transition row (None for
    rows created through a real path in this test).
    """
    db.refresh(action)
    rows = _rows(db, "recovery_action", action.id)
    assert rows, f"no audit rows at all for action {action.id}"

    # (a) a creation record exists for rows born on a real code path
    if creation is not None:
        assert any(r.action == creation for r in rows), (
            f"no creation record ({creation}) for action {action.id}: "
            f"{[r.action for r in rows]}"
        )

    transitions = [r for r in rows if (r.details or {}).get("to_status")]
    assert transitions, f"no transition rows for action {action.id}"

    # (b) contiguous chain: first row starts at the documented origin, each
    # subsequent row's from_status equals the previous row's to_status
    prev = first_from
    for row in transitions:
        d = row.details
        assert d.get("from_status") == prev, (
            f"audit chain broken at {row.action} (id {row.id}): "
            f"from_status={d.get('from_status')!r}, expected {prev!r}"
        )
        assert row.actor, f"audit row {row.id} has no actor"
        if request_id is not None and row.actor != "system:webhook":
            assert row.request_id == request_id, (
                f"audit row {row.id} ({row.action}) lost the request id"
            )
        prev = d["to_status"]

    # (c) the chain reaches the action's ACTUAL final status — no silent hop
    assert prev == final.value, (
        f"audit chain ends at {prev!r} but the action is {final.value!r}"
    )
    assert action.status is final
    return rows


def _auto_link_opportunity(db_session, make_opportunity, make_diagnosis, abandoned_payment):
    """Opportunity + its payment-link strategy, which auto-executes
    (abandonment class, 0.95 diagnosis). Returns (opp, strategy)."""
    from app.services.recovery.strategies import StrategyGenerator

    opp = make_opportunity(payment=abandoned_payment())
    make_diagnosis(db_session.get(models.Incident, opp.incident_id), confidence=0.95)
    strategy = next(
        s
        for s in StrategyGenerator(db_session).generate(opp)
        if s.action_type is ActionType.CREATE_PAYMENT_LINK
    )
    db_session.commit()
    return opp, strategy


def _approval_lane_opportunity(db_session, make_opportunity, abandoned_payment):
    """Opportunity + payment-link strategy that lands in the approval lane
    (no diagnosis -> 0.80 evidence confidence, below the 0.85 floor)."""
    from app.services.recovery.strategies import StrategyGenerator

    opp = make_opportunity(payment=abandoned_payment())
    strategy = next(
        s
        for s in StrategyGenerator(db_session).generate(opp)
        if s.action_type is ActionType.CREATE_PAYMENT_LINK
    )
    db_session.commit()
    return opp, strategy


# --- the sweep ----------------------------------------------------------------


class TestExecutorDrivenTransitions:
    def test_auto_execute_recovered(
        self, db_session, make_opportunity, make_diagnosis, abandoned_payment
    ):
        opp, strategy = _auto_link_opportunity(
            db_session, make_opportunity, make_diagnosis, abandoned_payment
        )
        action = RecoveryExecutor(
            db_session, CountingGateway(success_rate=1.0)
        ).execute(opp.id, strategy_id=strategy.id, actor=ACTOR, request_id="req-sweep-auto")
        db_session.commit()
        rows = assert_transition_audit(
            db_session, action, final=RecoveryStatus.RECOVERED, request_id="req-sweep-auto"
        )
        assert [r.action for r in rows] == [
            "recovery.action.proposed",
            "recovery.action.policy_evaluated",
            "recovery.action.executing",
            "recovery.action.verifying",
            "recovery.action.recovered",
        ]

    def test_blocked_opted_out_customer(
        self, db_session, make_customer, make_payment, make_opportunity
    ):
        customer = make_customer(opted_out=True)
        payment = make_payment(customer_id=customer.id, status="failed")
        opp = make_opportunity(payment=payment, customer=customer)
        action = RecoveryExecutor(
            db_session, CountingGateway(success_rate=1.0)
        ).execute(opp.id, actor=ACTOR, request_id="req-sweep-block")
        db_session.commit()
        rows = assert_transition_audit(
            db_session, action, final=RecoveryStatus.REJECTED, request_id="req-sweep-block"
        )
        assert [r.action for r in rows] == [
            "recovery.action.proposed",
            "recovery.action.policy_evaluated",
            "recovery.action.rejected",
        ]

    def test_approval_lane_full_chain(
        self, db_session, make_opportunity, abandoned_payment
    ):
        opp, strategy = _approval_lane_opportunity(
            db_session, make_opportunity, abandoned_payment
        )
        executor = RecoveryExecutor(db_session, CountingGateway(success_rate=1.0))
        pending = executor.execute(
            opp.id, strategy_id=strategy.id, actor=ACTOR, request_id="req-sweep-appr"
        )
        assert pending.status is RecoveryStatus.PENDING_APPROVAL
        executor.approve(opp.id, actor="human:approver", request_id="req-sweep-appr")
        final = executor.execute(
            opp.id, strategy_id=strategy.id, actor=ACTOR, request_id="req-sweep-appr"
        )
        db_session.commit()
        rows = assert_transition_audit(
            db_session, final, final=RecoveryStatus.RECOVERED, request_id="req-sweep-appr"
        )
        assert [r.action for r in rows] == [
            "recovery.action.proposed",
            "recovery.action.policy_evaluated",
            "recovery.action.pending_approval",
            "recovery.action.approved",
            "recovery.action.executing",
            "recovery.action.verifying",
            "recovery.action.recovered",
        ]

    def test_reject_from_pending_approval(
        self, db_session, make_opportunity, abandoned_payment
    ):
        opp, strategy = _approval_lane_opportunity(
            db_session, make_opportunity, abandoned_payment
        )
        executor = RecoveryExecutor(db_session, CountingGateway(success_rate=1.0))
        executor.execute(
            opp.id, strategy_id=strategy.id, actor=ACTOR, request_id="req-sweep-rej"
        )
        action = executor.reject(
            opp.id, actor=ACTOR, reason="not worth it", request_id="req-sweep-rej"
        )
        db_session.commit()
        assert_transition_audit(
            db_session, action, final=RecoveryStatus.REJECTED, request_id="req-sweep-rej"
        )

    def test_cancel_from_proposed(
        self, db_session, make_opportunity, make_proposed_action
    ):
        opp = make_opportunity()
        make_proposed_action(opp)  # seeded: creation bypasses real paths
        db_session.commit()
        action = RecoveryExecutor(
            db_session, CountingGateway(success_rate=1.0)
        ).cancel(opp.id, actor=ACTOR, reason="stale", request_id="req-sweep-cancel")
        db_session.commit()
        assert_transition_audit(
            db_session,
            action,
            final=RecoveryStatus.CANCELLED,
            first_from="PROPOSED",
            creation=None,
            request_id="req-sweep-cancel",
        )

    def test_escalate_from_proposed(
        self, db_session, make_opportunity, make_proposed_action
    ):
        opp = make_opportunity()
        make_proposed_action(opp)  # seeded: creation bypasses real paths
        db_session.commit()
        action = RecoveryExecutor(
            db_session, CountingGateway(success_rate=1.0)
        ).escalate(opp.id, actor=ACTOR, reason="vip", request_id="req-sweep-esc")
        db_session.commit()
        assert_transition_audit(
            db_session,
            action,
            final=RecoveryStatus.ESCALATED,
            first_from="PROPOSED",
            creation=None,
            request_id="req-sweep-esc",
        )

    def test_gateway_4xx_failed(
        self, db_session, make_opportunity, make_diagnosis, abandoned_payment
    ):
        opp, strategy = _auto_link_opportunity(
            db_session, make_opportunity, make_diagnosis, abandoned_payment
        )
        action = RecoveryExecutor(db_session, ConfusedGateway()).execute(
            opp.id, strategy_id=strategy.id, actor=ACTOR, request_id="req-sweep-4xx"
        )
        db_session.commit()
        rows = assert_transition_audit(
            db_session, action, final=RecoveryStatus.FAILED, request_id="req-sweep-4xx"
        )
        assert rows[-1].action == "recovery.action.failed"
        assert "GatewayBadRequestError" in (action.last_error or "")

    def test_gateway_timeout_unknown_then_still_unknown(
        self, db_session, make_payment, make_opportunity, make_diagnosis,
        abandoned_payment,
    ):
        opp, strategy = _auto_link_opportunity(
            db_session, make_opportunity, make_diagnosis, abandoned_payment
        )
        executor = RecoveryExecutor(db_session, TimeoutGateway(success_rate=1.0))
        action = executor.execute(
            opp.id, strategy_id=strategy.id, actor=ACTOR, request_id="req-sweep-to"
        )
        assert action.status is RecoveryStatus.UNKNOWN
        # resolve with no gateway truth available: stays UNKNOWN, and the
        # inconclusive re-query is itself audited
        executor.resolve(action.id, actor=ACTOR, request_id="req-sweep-to")
        db_session.commit()
        rows = assert_transition_audit(
            db_session, action, final=RecoveryStatus.UNKNOWN, request_id="req-sweep-to"
        )
        checks = [r for r in rows if r.action == "recovery.action.resolve_check"]
        assert checks and checks[-1].details.get("result") == "still_unknown"

    def test_unknown_resolved_recovered(
        self, db_session, make_payment, make_opportunity, make_diagnosis,
        abandoned_payment,
    ):
        payment = abandoned_payment(gateway_payment_id="pay_sweep_heals")
        opp = make_opportunity(payment=payment)
        make_diagnosis(db_session.get(models.Incident, opp.incident_id), confidence=0.95)
        from app.services.recovery.strategies import StrategyGenerator

        strategy = next(
            s
            for s in StrategyGenerator(db_session).generate(opp)
            if s.action_type is ActionType.CREATE_PAYMENT_LINK
        )
        db_session.commit()
        executor = RecoveryExecutor(db_session, TimeoutGateway(success_rate=1.0))
        action = executor.execute(
            opp.id, strategy_id=strategy.id, actor=ACTOR, request_id="req-sweep-res"
        )
        assert action.status is RecoveryStatus.UNKNOWN

        resolved = RecoveryExecutor(
            db_session, CapturedFetchGateway(success_rate=1.0)
        ).resolve(action.id, actor=ACTOR, request_id="req-sweep-res")
        db_session.commit()
        rows = assert_transition_audit(
            db_session, resolved, final=RecoveryStatus.RECOVERED, request_id="req-sweep-res"
        )
        assert rows[-1].action == "recovery.action.recovered"
        assert rows[-1].details.get("verification") == "fetch_payment"

    def test_in_flight_refusal_writes_no_new_rows(
        self, db_session, make_opportunity, make_proposed_action
    ):
        opp = make_opportunity()
        action = make_proposed_action(opp, status=RecoveryStatus.VERIFYING)  # seeded
        db_session.commit()
        before = len(_rows(db_session, "recovery_action", action.id))
        with pytest.raises(InvalidStateError, match="in flight"):
            RecoveryExecutor(db_session, CountingGateway(success_rate=1.0)).execute(
                opp.id, actor=ACTOR, request_id="req-sweep-refused"
            )
        db_session.commit()
        after = _rows(db_session, "recovery_action", action.id)
        assert len(after) == before, "a refused execute must not mutate the trail"
        assert action.status is RecoveryStatus.VERIFYING


class TestAgentToolCreatedRows:
    def test_agent_requested_row_then_execute(
        self, db_session, make_incident, make_payment
    ):
        """Agent tools create the PROPOSED row + evaluate (single
        agent.action_requested audit record); the executor then re-gates and
        fires. The from/to chain starts where the tool left the action
        (POLICY_EVALUATED) and must reach RECOVERED without gaps."""
        incident = make_incident()
        payment = make_payment(amount_paise=100_000, status="failed")
        tools = AgentTools(db_session, incident_id=incident.id)
        result = tools.call(
            "request_payment_link", {"payment_id": payment.id, "confidence": 0.99}
        )
        db_session.commit()
        action = db_session.get(models.RecoveryAction, result.data["action_id"])
        assert action.status is RecoveryStatus.POLICY_EVALUATED

        fired = RecoveryExecutor(
            db_session, CountingGateway(success_rate=1.0)
        ).execute(action.opportunity_id, actor=ACTOR, request_id="req-sweep-agent")
        db_session.commit()
        rows = assert_transition_audit(
            db_session,
            fired,
            final=RecoveryStatus.RECOVERED,
            first_from="POLICY_EVALUATED",  # the state the tool left it in
            creation="agent.action_requested",
            request_id="req-sweep-agent",
        )


class TestWebhookDrivenTransitions:
    def test_webhook_recovers_verifying_action(
        self, client, sign, db_session, gateway, make_opportunity,
        make_diagnosis, failed_payment,
    ):
        payment = failed_payment(gateway_payment_id="pay_sweep_hook")
        opp = make_opportunity(payment=payment)
        make_diagnosis(db_session.get(models.Incident, opp.incident_id), confidence=0.95)
        db_session.commit()
        action = RecoveryExecutor(db_session, gateway).execute(
            opp.id, actor=ACTOR, request_id="req-sweep-hook"
        )
        db_session.commit()
        # recommended strategy is retry_payment -> fresh order; orders never
        # pay inline, so the action awaits webhook/fetch truth
        assert action.action_type is ActionType.RETRY_PAYMENT
        assert action.status is RecoveryStatus.VERIFYING

        body = json.dumps(
            {
                "entity": "event",
                "event": "payment.captured",
                "contains": ["payment"],
                "payload": {
                    "payment": {
                        "entity": {
                            "id": "pay_sweep_hook",
                            "entity": "payment",
                            "amount": 100_000,
                            "currency": "INR",
                            "status": "captured",
                            "method": "upi",
                            "captured": True,
                        }
                    }
                },
                "created_at": 1700000000,
            }
        ).encode()
        r = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "x-razorpay-signature": sign(body),
                "x-razorpay-event-id": "evt_sweep_hook",
            },
        )
        assert r.status_code == 200 and r.json()["processed"] is True
        db_session.commit()
        rows = assert_transition_audit(
            db_session, action, final=RecoveryStatus.RECOVERED
        )
        webhook_rows = [r for r in rows if r.actor == "system:webhook"]
        assert len(webhook_rows) == 1
        assert webhook_rows[0].details["from_status"] == "VERIFYING"
        assert webhook_rows[0].details["to_status"] == "RECOVERED"


class TestOpportunityLevelTransitions:
    def test_opportunity_level_reject_audited(
        self, db_session, make_opportunity
    ):
        opp = make_opportunity()
        result = RecoveryExecutor(
            db_session, CountingGateway(success_rate=1.0)
        ).reject(opp.id, actor=ACTOR, reason="noise", request_id="req-sweep-opp")
        db_session.commit()
        assert result is None
        db_session.refresh(opp)
        assert opp.status is RecoveryStatus.REJECTED
        rows = _rows(db_session, "recovery_opportunity", opp.id)
        assert [r.action for r in rows] == ["recovery.opportunity.rejected"]
        assert rows[0].details["from_status"] == "PROPOSED"
        assert rows[0].details["to_status"] == "REJECTED"
        assert rows[0].actor == ACTOR
        assert rows[0].request_id == "req-sweep-opp"

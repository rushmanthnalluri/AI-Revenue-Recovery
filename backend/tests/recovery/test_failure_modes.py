"""Failure-scenario proofs — the recovery engine's safety story as executable
tests. Each test maps to one guarantee from docs/recovery.md:

1. Gateway timeout -> UNKNOWN, duplicate protection active, re-execute makes
   no new mutation, later fetch resolves truthfully.
2. AI-proposed refund -> policy BLOCKED with ZERO gateway calls.
3. Duplicate execute -> exactly one gateway mutation.
4. confidence < 0.85 -> PENDING_APPROVAL; execute refused until approve.
5. Stopping rule -> after 3 consecutive FAILED actions on an incident, the
   next action is BLOCKED.
"""

import httpx
import pytest

import app.models as models
from app.ports import ActionType, PolicyOutcome, RecoveryStatus
from app.services.razorpay.client import RazorpayGateway
from app.services.recovery import InvalidStateError

ACTOR = "human:console"


class _RecordingTransport:
    """Programmable httpx transport; records every request it sees."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.payment_status = "failed"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST":
            # The gateway dies mid-mutation: no authoritative response.
            raise httpx.ConnectError("connection dropped", request=request)
        if request.url.path.startswith("/v1/payments/"):
            return httpx.Response(
                200,
                json={
                    "id": request.url.path.rsplit("/", 1)[-1],
                    "entity": "payment",
                    "status": self.payment_status,
                    "captured": self.payment_status == "captured",
                    "amount": 100_000,
                    "currency": "INR",
                },
            )
        return httpx.Response(404, json={"error": {"description": "not found"}})

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    @property
    def gets(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]


def _mock_gateway(transport: _RecordingTransport) -> RazorpayGateway:
    return RazorpayGateway(
        key_id="rzp_test_key",
        key_secret="secret",
        transport=httpx.MockTransport(transport.handler),
        sleep=lambda _: None,
    )


class TestTimeoutUnknownResolution:
    """(1) timeout -> UNKNOWN; duplicate protection; no blind retry; fetch
    resolves the truth later."""

    def test_full_scenario(
        self, db_session, make_executor, make_opportunity, make_customer,
        make_proposed_action, failed_payment,
    ):
        transport = _RecordingTransport()
        gateway = _mock_gateway(transport)
        customer = make_customer()
        payment = failed_payment(customer_id=customer.id, gateway_payment_id="pay_t1")
        opp = make_opportunity(payment=payment, customer=customer)
        proposed = make_proposed_action(
            opp,
            action_type=ActionType.RETRY_PAYMENT,
            confidence=0.95,
            gateway_request_id="gwr_timeoutcase01",
        )
        db_session.commit()
        executor = make_executor(gateway)

        # -- fire: the POST dies in flight -> UNKNOWN, exactly one mutation ---
        action = executor.execute(opp.id, actor=ACTOR)
        assert action.status is RecoveryStatus.UNKNOWN
        assert action.attempts == 1
        assert len(transport.posts) == 1
        assert "GatewayTransientError" in (action.last_error or "")

        # -- re-execute: no blind retry; resolve re-queries (GET only) --------
        again = executor.execute(opp.id, actor=ACTOR)
        assert again.id == action.id
        assert again.status is RecoveryStatus.UNKNOWN  # payment still failed
        assert len(transport.posts) == 1  # NO second mutation
        assert len(transport.gets) >= 1  # truth was re-queried instead

        # -- duplicate protection: a parallel proposal for the same customer +
        # action type is BLOCKED while the UNKNOWN action is still active -----
        opp2 = make_opportunity(payment=failed_payment(customer_id=customer.id), customer=customer)
        make_proposed_action(
            opp2,
            action_type=ActionType.RETRY_PAYMENT,
            confidence=0.95,
            gateway_request_id="gwr_timeoutcase02",
        )
        db_session.commit()
        dup = executor.execute(opp2.id, actor=ACTOR)
        assert dup.status is RecoveryStatus.REJECTED  # blocked by policy
        decision = db_session.get(models.PolicyDecisionRecord, dup.policy_decision_id)
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "duplicate.cooldown" in decision.rules_matched
        assert len(transport.posts) == 1  # still exactly one gateway mutation

        # -- the payment later captures at the gateway; fetch resolves it -----
        transport.payment_status = "captured"
        resolved = executor.execute(opp.id, actor=ACTOR)
        assert resolved.id == action.id
        assert resolved.status is RecoveryStatus.RECOVERED
        assert resolved.verified_at is not None
        assert len(transport.posts) == 1  # resolution never re-fired the mutation

        # resolve audit evidence exists for both the inconclusive and the
        # decisive re-query
        checks = [
            r
            for r in db_session.query(models.AuditLog)
            .filter_by(entity_type="recovery_action", entity_id=action.id)
            .all()
            if r.action == "recovery.action.resolve_check"
        ]
        assert any(r.details.get("result") == "still_unknown" for r in checks)

    def test_resolve_rejects_non_unknown_actions(
        self, db_session, sim_gateway, make_executor, make_opportunity,
        make_proposed_action,
    ):
        opp = make_opportunity()
        action = make_proposed_action(opp)
        db_session.commit()
        with pytest.raises(InvalidStateError, match="not UNKNOWN"):
            make_executor(sim_gateway).resolve(action.id, actor=ACTOR)


class TestRefundHasNoExecutionPath:
    """(2) An AI-proposed refund is BLOCKED by policy with zero gateway calls."""

    def test_refund_blocked_and_gateway_never_touched(
        self, db_session, make_executor, make_opportunity, make_proposed_action
    ):
        transport = _RecordingTransport()
        gateway = _mock_gateway(transport)
        opp = make_opportunity()
        make_proposed_action(
            opp,
            action_type=ActionType.REFUND,
            confidence=0.99,
            amount_paise=100,
            actor="agent:llm-gpt",
        )
        db_session.commit()

        action = make_executor(gateway).execute(opp.id, actor=ACTOR)

        assert action.status is RecoveryStatus.REJECTED
        assert transport.requests == []  # not a single gateway call
        assert action.attempts == 0
        decision = db_session.get(models.PolicyDecisionRecord, action.policy_decision_id)
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "allowlist" in decision.rules_matched
        assert "never_auto_execute.refund" in decision.rules_matched
        # the block itself is in the append-only audit trail
        blocked_audits = (
            db_session.query(models.AuditLog)
            .filter_by(action="policy.action_blocked")
            .all()
        )
        assert len(blocked_audits) == 1


class TestDuplicateExecute:
    """(3) A duplicate execute request results in exactly one gateway call."""

    def test_second_execute_is_policy_blocked_after_recovery(
        self, db_session, sim_gateway, make_executor, make_opportunity,
        make_customer, make_diagnosis, abandoned_payment,
    ):
        from app.services.recovery.strategies import StrategyGenerator

        customer = make_customer()
        payment = abandoned_payment(customer_id=customer.id)
        opp = make_opportunity(payment=payment, customer=customer)
        make_diagnosis(db_session.get(models.Incident, opp.incident_id), confidence=0.95)
        link_strategy = next(
            s
            for s in StrategyGenerator(db_session).generate(opp)
            if s.action_type is ActionType.CREATE_PAYMENT_LINK
        )
        db_session.commit()
        executor = make_executor(sim_gateway)

        first = executor.execute(opp.id, strategy_id=link_strategy.id, actor=ACTOR)
        assert first.status is RecoveryStatus.RECOVERED  # link paid inline
        assert len(sim_gateway.payment_links) == 1

        # duplicate execute on the SAME opportunity + SAME strategy: the
        # recovered action is terminal, so a new action is proposed — and
        # policy's duplicate guard (RECOVERED stays "active" for cooldown)
        # BLOCKS it before the gateway.
        second = executor.execute(opp.id, strategy_id=link_strategy.id, actor=ACTOR)
        assert second.id != first.id
        assert second.status is RecoveryStatus.REJECTED
        decision = db_session.get(models.PolicyDecisionRecord, second.policy_decision_id)
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "duplicate.cooldown" in decision.rules_matched
        assert len(sim_gateway.payment_links) == 1  # exactly one gateway call

        db_session.refresh(opp)
        assert opp.status is RecoveryStatus.REJECTED  # shadows the latest action


class TestApprovalGate:
    """(4) confidence < 0.85 -> PENDING_APPROVAL; execute refused until approve."""

    def test_confidence_below_floor_requires_human(
        self, db_session, sim_gateway, make_executor, make_opportunity,
        make_proposed_action,
    ):
        opp = make_opportunity()
        make_proposed_action(
            opp, action_type=ActionType.CREATE_PAYMENT_LINK, confidence=0.80
        )
        db_session.commit()
        executor = make_executor(sim_gateway)

        action = executor.execute(opp.id, actor=ACTOR)
        assert action.status is RecoveryStatus.PENDING_APPROVAL
        assert len(sim_gateway.payment_links) == 0  # refused: nothing fired

        with pytest.raises(InvalidStateError, match="await"):
            executor.execute(opp.id, actor=ACTOR)
        assert len(sim_gateway.payment_links) == 0  # still refused

        executor.approve(opp.id, actor="human:ops", note="manual review ok")
        final = executor.execute(opp.id, actor=ACTOR)
        assert final.status is RecoveryStatus.RECOVERED
        assert len(sim_gateway.payment_links) == 1  # fired exactly once, post-approval


class TestStoppingRule:
    """(5) Three consecutive FAILED actions on an incident halt automation."""

    def test_fourth_action_is_blocked_after_three_failures(
        self, db_session, stub_gateway, make_executor, make_opportunity,
        make_customer, make_diagnosis, failed_payment, make_incident,
    ):
        incident = make_incident()
        make_diagnosis(incident, confidence=0.95)
        executor = make_executor(stub_gateway)

        actions = []
        for _ in range(3):
            customer = make_customer()
            opp = make_opportunity(
                incident=incident, payment=failed_payment(customer_id=customer.id)
            )
            db_session.commit()
            action = executor.execute(opp.id, actor=ACTOR)
            # gateway definitively rejects (4xx) -> FAILED, attempt consumed
            assert action.status is RecoveryStatus.FAILED
            actions.append(action)
        assert stub_gateway.mutation_calls == 3

        customer4 = make_customer()
        opp4 = make_opportunity(
            incident=incident, payment=failed_payment(customer_id=customer4.id)
        )
        db_session.commit()
        blocked = executor.execute(opp4.id, actor=ACTOR)

        assert blocked.status is RecoveryStatus.REJECTED
        decision = db_session.get(models.PolicyDecisionRecord, blocked.policy_decision_id)
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "stopping_rule.incident" in decision.rules_matched
        assert stub_gateway.mutation_calls == 3  # the 4th never reached the gateway
        # and the failed streak is intact: three FAILED, no successes between
        assert all(a.status is RecoveryStatus.FAILED for a in actions)

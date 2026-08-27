"""Policy engine proofs — PulseRecover's safety story as executable tests.

Each test class maps to one guarantee of the deterministic gate (ADR 0003).
"""

from datetime import timedelta
import inspect

import pytest
import sqlalchemy as sa

import app.models as models
from app.db import utcnow
from app.ports import ActionType, PolicyOutcome, RecoveryStatus
from app.services.policy import PolicyEngine
from app.services.policy.config import KillSwitchConfig


class TestContract:
    def test_satisfies_policy_engine_proto(self, engine):
        # ports.PolicyEngineProto is not @runtime_checkable (isinstance would
        # raise TypeError), so verify the structural contract directly.
        evaluate = getattr(engine, "evaluate", None)
        assert callable(evaluate)
        assert "ctx" in inspect.signature(evaluate).parameters


class TestHardBlocks:
    """(1) An AI-proposed refund is BLOCKED, with no execution path at all."""

    def test_ai_proposed_refund_blocked(self, engine, make_ctx):
        ctx = make_ctx(
            action_type=ActionType.REFUND,
            amount_paise=100,  # tiny amount, huge confidence — still blocked
            confidence=0.99,
            actor="agent:llm-gpt",
        )
        decision = engine.evaluate(ctx)
        assert decision.outcome is PolicyOutcome.BLOCKED
        # No execution path: neither auto-execute nor the human-approval lane.
        assert decision.outcome is not PolicyOutcome.ALLOWED
        assert decision.outcome is not PolicyOutcome.REQUIRES_APPROVAL
        assert "never_auto_execute.refund" in decision.rules_matched

    def test_refund_is_not_even_on_the_allowlist(self, engine):
        assert ActionType.REFUND.value not in engine.config.allowlist

    def test_refund_as_raw_string_is_coerced_then_blocked(self, engine, make_ctx):
        decision = engine.evaluate(make_ctx(action_type="refund"))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "never_auto_execute.refund" in decision.rules_matched

    def test_irreversible_action_blocked(self, engine, make_ctx):
        decision = engine.evaluate(make_ctx(metadata={"irreversible_action": True}))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "never_auto_execute.irreversible_action" in decision.rules_matched


class TestApprovalThresholds:
    """(2) amount > Rs 5000 and (3) confidence < 0.85 force human approval."""

    def test_amount_above_5000_inr_requires_approval(self, engine, make_ctx):
        decision = engine.evaluate(make_ctx(amount_paise=500_001))  # Rs 5000.01
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert "approval.amount" in decision.rules_matched

    def test_amount_exactly_5000_inr_is_within_bounds(self, engine, make_ctx):
        decision = engine.evaluate(make_ctx(amount_paise=500_000))
        assert "approval.amount" not in decision.rules_matched
        assert decision.outcome is PolicyOutcome.ALLOWED

    def test_confidence_below_085_requires_approval(self, engine, make_ctx):
        decision = engine.evaluate(make_ctx(confidence=0.84))
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert "approval.confidence" in decision.rules_matched

    def test_confidence_exactly_085_is_within_bounds(self, engine, make_ctx):
        decision = engine.evaluate(make_ctx(confidence=0.85))
        assert "approval.confidence" not in decision.rules_matched
        assert decision.outcome is PolicyOutcome.ALLOWED

    def test_attempt_budget_exhausted_requires_approval(self, engine, make_ctx):
        decision = engine.evaluate(make_ctx(attempts_so_far=2))  # max_attempts: 2
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert "approval.attempts" in decision.rules_matched

    def test_approval_is_not_execution(self, engine, make_ctx):
        decision = engine.evaluate(make_ctx(amount_paise=9_999_900, confidence=0.1))
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert decision.outcome is not PolicyOutcome.ALLOWED


class TestOptedOutCustomer:
    """(4) An opted-out customer is never auto-executed against."""

    def test_opted_out_customer_blocked(self, engine, make_ctx):
        decision = engine.evaluate(
            make_ctx(customer_id="cus_abc", customer_opted_out=True)
        )
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "never_auto_execute.customer_opted_out" in decision.rules_matched

    def test_opted_out_blocks_even_high_confidence_small_amount(self, engine, make_ctx):
        ctx = make_ctx(customer_opted_out=True, confidence=1.0, amount_paise=100)
        assert engine.evaluate(ctx).outcome is PolicyOutcome.BLOCKED


class TestStoppingRules:
    """(5) Automation halts after 3 consecutive FAILED actions."""

    def test_stopping_rule_fires_from_context_signal(self, engine, make_ctx, make_incident):
        incident = make_incident()
        decision = engine.evaluate(
            make_ctx(incident_id=incident.id, consecutive_failures=3)
        )
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "stopping_rule.incident" in decision.rules_matched

    def test_stopping_rule_fires_from_recorded_history(
        self, engine, make_ctx, make_incident, make_recovery_action
    ):
        incident = make_incident()
        base = utcnow()
        for minutes_ago in (3, 2, 1):
            make_recovery_action(
                incident=incident,
                status=RecoveryStatus.FAILED,
                created_at=base - timedelta(minutes=minutes_ago),
            )
        # Caller passes consecutive_failures=0 — the DB history still trips it.
        decision = engine.evaluate(make_ctx(incident_id=incident.id, consecutive_failures=0))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "stopping_rule.incident" in decision.rules_matched

    def test_two_consecutive_failures_do_not_trip(self, engine, make_ctx, make_incident):
        incident = make_incident()
        decision = engine.evaluate(
            make_ctx(incident_id=incident.id, consecutive_failures=2)
        )
        assert "stopping_rule.incident" not in decision.rules_matched
        assert decision.outcome is PolicyOutcome.ALLOWED

    def test_stopping_rule_per_strategy(
        self, engine, make_ctx, make_recovery_action, db_session
    ):
        base = utcnow()
        first = make_recovery_action(
            status=RecoveryStatus.FAILED, created_at=base - timedelta(minutes=3)
        )
        strategy = db_session.get(models.RecoveryStrategy, first.strategy_id)
        make_recovery_action(
            strategy=strategy,
            status=RecoveryStatus.FAILED,
            created_at=base - timedelta(minutes=2),
        )
        make_recovery_action(
            strategy=strategy,
            status=RecoveryStatus.FAILED,
            created_at=base - timedelta(minutes=1),
        )
        decision = engine.evaluate(make_ctx(metadata={"strategy_id": strategy.id}))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "stopping_rule.strategy" in decision.rules_matched

    def test_a_recovery_breaks_the_strategy_streak(
        self, engine, make_ctx, make_recovery_action, db_session
    ):
        base = utcnow()
        first = make_recovery_action(
            status=RecoveryStatus.FAILED, created_at=base - timedelta(minutes=3)
        )
        strategy = db_session.get(models.RecoveryStrategy, first.strategy_id)
        make_recovery_action(
            strategy=strategy,
            status=RecoveryStatus.FAILED,
            created_at=base - timedelta(minutes=2),
        )
        make_recovery_action(
            strategy=strategy,
            status=RecoveryStatus.RECOVERED,  # newest action succeeded
            created_at=base - timedelta(minutes=1),
        )
        decision = engine.evaluate(make_ctx(metadata={"strategy_id": strategy.id}))
        assert "stopping_rule.strategy" not in decision.rules_matched

    def test_escalation_still_allowed_when_stopping_rule_tripped(
        self, engine, make_ctx, make_incident
    ):
        incident = make_incident()
        decision = engine.evaluate(
            make_ctx(
                action_type=ActionType.ESCALATE_HUMAN,
                incident_id=incident.id,
                consecutive_failures=99,
            )
        )
        assert decision.outcome is PolicyOutcome.ALLOWED


class TestDuplicateProtection:
    """(6) Same customer + same action type within the cooldown -> BLOCKED."""

    def test_duplicate_blocked_within_cooldown(
        self, engine, make_ctx, make_customer, make_recovery_action
    ):
        customer = make_customer()
        make_recovery_action(
            customer=customer,
            status=RecoveryStatus.EXECUTING,
            created_at=utcnow() - timedelta(minutes=10),  # cooldown: 60 min
        )
        decision = engine.evaluate(make_ctx(customer_id=customer.id))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "duplicate.cooldown" in decision.rules_matched

    def test_recovered_action_still_blocks_duplicates(
        self, engine, make_ctx, make_customer, make_recovery_action
    ):
        """Never double-collect: a RECOVERED retry must not be re-fired."""
        customer = make_customer()
        make_recovery_action(
            customer=customer,
            status=RecoveryStatus.RECOVERED,
            created_at=utcnow() - timedelta(minutes=10),
        )
        decision = engine.evaluate(make_ctx(customer_id=customer.id))
        assert "duplicate.cooldown" in decision.rules_matched
        assert decision.outcome is PolicyOutcome.BLOCKED

    def test_failed_action_does_not_block_reproposal(
        self, engine, make_ctx, make_customer, make_recovery_action
    ):
        customer = make_customer()
        make_recovery_action(
            customer=customer,
            status=RecoveryStatus.FAILED,  # conclusively ended: may re-propose
            created_at=utcnow() - timedelta(minutes=10),
        )
        decision = engine.evaluate(make_ctx(customer_id=customer.id))
        assert "duplicate.cooldown" not in decision.rules_matched

    def test_duplicate_window_expires(
        self, engine, make_ctx, make_customer, make_recovery_action
    ):
        customer = make_customer()
        make_recovery_action(
            customer=customer,
            status=RecoveryStatus.EXECUTING,
            created_at=utcnow() - timedelta(minutes=120),  # outside the 60 min cooldown
        )
        decision = engine.evaluate(make_ctx(customer_id=customer.id))
        assert "duplicate.cooldown" not in decision.rules_matched
        assert decision.outcome is PolicyOutcome.ALLOWED

    def test_different_action_type_is_not_a_duplicate(
        self, engine, make_ctx, make_customer, make_recovery_action
    ):
        customer = make_customer()
        make_recovery_action(
            customer=customer,
            action_type=ActionType.CREATE_PAYMENT_LINK,
            status=RecoveryStatus.EXECUTING,
            created_at=utcnow() - timedelta(minutes=10),
        )
        decision = engine.evaluate(
            make_ctx(customer_id=customer.id, action_type=ActionType.RETRY_PAYMENT)
        )
        assert "duplicate.cooldown" not in decision.rules_matched

    def test_current_action_never_counts_against_itself(
        self, engine, make_ctx, make_customer, make_recovery_action
    ):
        """The action under evaluation may already be persisted as PROPOSED;
        it must not trip its own duplicate/rate-limit guards."""
        customer = make_customer()
        current = make_recovery_action(
            customer=customer,
            status=RecoveryStatus.PROPOSED,
            created_at=utcnow() - timedelta(minutes=1),
        )
        decision = engine.evaluate(
            make_ctx(
                customer_id=customer.id,
                metadata={"current_action_id": current.id},
            )
        )
        assert "duplicate.cooldown" not in decision.rules_matched
        assert decision.outcome is PolicyOutcome.ALLOWED


class TestRateLimits:
    def test_customer_daily_limit(
        self, engine, make_ctx, make_customer, make_recovery_action
    ):
        customer = make_customer()
        for action_type in (
            ActionType.CREATE_PAYMENT_LINK,
            ActionType.NOTIFY_CUSTOMER,
            ActionType.PAUSE_SUBSCRIPTION,
        ):
            make_recovery_action(customer=customer, action_type=action_type)
        decision = engine.evaluate(make_ctx(customer_id=customer.id))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "rate_limit.customer_daily" in decision.rules_matched

    def test_under_the_daily_limit(
        self, engine, make_ctx, make_customer, make_recovery_action
    ):
        customer = make_customer()
        make_recovery_action(customer=customer, action_type=ActionType.CREATE_PAYMENT_LINK)
        make_recovery_action(customer=customer, action_type=ActionType.NOTIFY_CUSTOMER)
        decision = engine.evaluate(make_ctx(customer_id=customer.id))
        assert "rate_limit.customer_daily" not in decision.rules_matched
        assert decision.outcome is PolicyOutcome.ALLOWED

    def test_rejected_actions_do_not_consume_budget(
        self, engine, make_ctx, make_customer, make_recovery_action
    ):
        customer = make_customer()
        for _ in range(3):  # policy-rejected proposals never reached the gateway
            make_recovery_action(customer=customer, status=RecoveryStatus.REJECTED)
        decision = engine.evaluate(make_ctx(customer_id=customer.id))
        assert "rate_limit.customer_daily" not in decision.rules_matched

    def test_incident_action_budget(self, engine, make_ctx, make_incident, make_recovery_action):
        incident = make_incident()
        for _ in range(10):  # max_actions_per_incident: 10
            make_recovery_action(incident=incident)
        decision = engine.evaluate(make_ctx(incident_id=incident.id))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "rate_limit.incident" in decision.rules_matched


class TestKillSwitch:
    """Global kill switch: config-level emergency brake."""

    @pytest.fixture()
    def killed_engine(self, policy_config, db_session):
        config = policy_config.model_copy(
            update={
                "kill_switch": KillSwitchConfig(enabled=True, reason="incident response")
            }
        )
        return PolicyEngine(config, session=db_session)

    def test_kill_switch_blocks_financial_actions(self, killed_engine, make_ctx):
        decision = killed_engine.evaluate(make_ctx())
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "kill_switch" in decision.rules_matched
        assert "incident response" in decision.reasons[0]

    def test_escape_hatches_stay_open(self, killed_engine, make_ctx):
        assert (
            killed_engine.evaluate(make_ctx(action_type=ActionType.ESCALATE_HUMAN)).outcome
            is PolicyOutcome.ALLOWED
        )
        assert (
            killed_engine.evaluate(make_ctx(action_type=ActionType.NO_ACTION)).outcome
            is PolicyOutcome.ALLOWED
        )


class TestSafeActions:
    def test_no_action_always_allowed(self, engine, make_ctx):
        decision = engine.evaluate(
            make_ctx(
                action_type=ActionType.NO_ACTION,
                amount_paise=9_999_999,
                confidence=0.0,
                customer_opted_out=True,
                consecutive_failures=99,
            )
        )
        assert decision.outcome is PolicyOutcome.ALLOWED
        assert "safe_action" in decision.rules_matched

    def test_escalate_allowed_despite_bad_context(self, engine, make_ctx):
        decision = engine.evaluate(
            make_ctx(action_type=ActionType.ESCALATE_HUMAN, attempts_so_far=99)
        )
        assert decision.outcome is PolicyOutcome.ALLOWED


class TestCompliantActionAllowed:
    """(7) A compliant low-risk action is ALLOWED, with reasons."""

    def test_compliant_action_allowed(self, engine, make_ctx):
        decision = engine.evaluate(
            make_ctx(
                action_type=ActionType.RETRY_PAYMENT,
                amount_paise=25_000,  # INR 250
                confidence=0.97,
                attempts_so_far=0,
            )
        )
        assert decision.outcome is PolicyOutcome.ALLOWED
        assert "auto_execute.ok" in decision.rules_matched
        assert decision.reasons and all(isinstance(r, str) for r in decision.reasons)
        assert decision.policy_version == engine.policy_version
        assert decision.policy_version.startswith("1.0+sha256.")


class TestDeterminism:
    """(8) Same context -> same decision, every time, on any engine instance."""

    def _fingerprint(self, decision):
        # decided_at is a wall-clock stamp and intentionally excluded.
        return (
            decision.outcome,
            tuple(decision.reasons),
            tuple(decision.rules_matched),
            decision.policy_version,
        )

    def test_repeat_evaluation_is_identical(self, engine, make_ctx):
        ctx = make_ctx()
        assert self._fingerprint(engine.evaluate(ctx)) == self._fingerprint(
            engine.evaluate(ctx)
        )

    def test_deterministic_across_engine_instances(
        self, engine, policy_config, db_session, make_ctx
    ):
        other = PolicyEngine(policy_config, session=db_session)
        for ctx in (
            make_ctx(),
            make_ctx(action_type=ActionType.REFUND),
            make_ctx(amount_paise=9_999_999, confidence=0.1),
            make_ctx(customer_opted_out=True),
            make_ctx(consecutive_failures=5),
        ):
            assert self._fingerprint(engine.evaluate(ctx)) == self._fingerprint(
                other.evaluate(ctx)
            )


class TestFailClosed:
    """(9) Malformed input, unknown actions, broken config: nothing executes."""

    @pytest.mark.parametrize("bad_type", ["refund_now", "launch_nukes", "REFUND!!", 123, None])
    def test_unknown_action_type_blocked(self, engine, make_ctx, bad_type):
        decision = engine.evaluate(make_ctx(action_type=bad_type))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "malformed.action_type" in decision.rules_matched

    @pytest.mark.parametrize("bad_confidence", [float("nan"), float("inf"), -0.1, 1.01, "high"])
    def test_invalid_confidence_blocked(self, engine, make_ctx, bad_confidence):
        decision = engine.evaluate(make_ctx(confidence=bad_confidence))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "malformed.confidence" in decision.rules_matched

    @pytest.mark.parametrize("bad_amount", [-1, 100.5, "lots"])
    def test_invalid_amount_blocked(self, engine, make_ctx, bad_amount):
        decision = engine.evaluate(make_ctx(amount_paise=bad_amount))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "malformed.amount" in decision.rules_matched

    def test_non_inr_currency_blocked(self, engine, make_ctx):
        decision = engine.evaluate(make_ctx(currency="USD"))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "malformed.currency" in decision.rules_matched

    def test_evaluate_is_total_even_for_garbage(self, engine):
        decision = engine.evaluate(None)  # type: ignore[arg-type]
        assert decision.outcome is PolicyOutcome.BLOCKED

    def test_engine_without_history_never_auto_executes(self, policy_config, make_ctx):
        """No session/history -> stateful guards unverifiable -> at best
        REQUIRES_APPROVAL. Preview mode can never self-authorize execution."""
        engine = PolicyEngine(policy_config)
        decision = engine.evaluate(make_ctx())
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert "stateful.unverified" in decision.rules_matched
        # Context-only hard blocks still apply without any history.
        assert (
            engine.evaluate(make_ctx(action_type=ActionType.REFUND)).outcome
            is PolicyOutcome.BLOCKED
        )

    def test_failsafe_engine_blocks_everything(self, db_session, make_ctx):
        engine = PolicyEngine.failsafe("test: broken config", session=db_session)
        assert engine.policy_version == "failsafe"
        for ctx in (make_ctx(), make_ctx(action_type=ActionType.NO_ACTION)):
            decision = engine.evaluate(ctx)
            assert decision.outcome is PolicyOutcome.BLOCKED
            assert "kill_switch" in decision.rules_matched


class TestPersistenceAndAudit:
    def test_every_decision_is_persisted(self, engine, db_session, make_ctx):
        engine.evaluate(make_ctx(metadata={"current_action_id": "act_demo"}))
        engine.evaluate(make_ctx(action_type=ActionType.REFUND))
        rows = db_session.scalars(sa.select(models.PolicyDecisionRecord)).all()
        assert len(rows) == 2
        by_type = {row.action_type: row for row in rows}

        allowed = by_type["retry_payment"]
        assert allowed.outcome is PolicyOutcome.ALLOWED
        assert allowed.action_id == "act_demo"
        assert allowed.actor == "agent:strategist"
        assert allowed.amount_paise == 10_000
        assert allowed.policy_version == engine.policy_version
        assert allowed.context["action_type"] == "retry_payment"
        assert allowed.decided_at.tzinfo is not None

        blocked = by_type["refund"]
        assert blocked.outcome is PolicyOutcome.BLOCKED
        assert "never_auto_execute.refund" in blocked.rules_matched
        assert blocked.reasons

    def test_blocked_decisions_are_mirrored_to_audit_logs(
        self, engine, db_session, make_ctx
    ):
        engine.evaluate(
            make_ctx(action_type=ActionType.REFUND, metadata={"request_id": "req-test-1"})
        )
        rows = db_session.scalars(sa.select(models.AuditLog)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.action == "policy.action_blocked"
        assert row.entity_type == "policy_decision"
        assert row.actor == "agent:strategist"
        assert row.request_id == "req-test-1"
        assert "never_auto_execute.refund" in row.details["rules_matched"]

    def test_allowed_decisions_are_not_mirrored(self, engine, db_session, make_ctx):
        engine.evaluate(make_ctx())
        assert db_session.scalars(sa.select(models.AuditLog)).all() == []

    def test_persistence_flushes_but_never_commits(
        self, engine, db_session, make_ctx
    ):
        """The caller owns the transaction boundary."""
        engine.evaluate(make_ctx())
        db_session.rollback()
        assert db_session.scalars(sa.select(models.PolicyDecisionRecord)).all() == []

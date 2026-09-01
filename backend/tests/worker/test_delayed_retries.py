"""Delayed retries: the executor parks a delay_seconds strategy in SCHEDULED
and the worker fires it when due — never before, and exactly once."""

import sqlalchemy as sa

import app.models as models
from app.ports import ActionType, RecoveryStatus
from app.services.recovery import RecoveryExecutor
from app.services.recovery.strategies import StrategyGenerator

ACTOR = "human:console"


def _audit_actions(db_session, action_id):
    return [
        r.action
        for r in db_session.query(models.AuditLog)
        .filter_by(entity_type="recovery_action", entity_id=action_id)
        .order_by(models.AuditLog.created_at, models.AuditLog.id)
        .all()
    ]


class TestParkOnExecute:
    def test_delayed_strategy_parks_instead_of_firing(
        self, db_session, sim_gateway, fake_clock, make_opportunity, make_strategy,
        failed_payment,
    ):
        opp = make_opportunity(payment=failed_payment())
        strategy = make_strategy(opp, constraints={"delay_seconds": 1800})

        executor = RecoveryExecutor(db_session, sim_gateway, clock=fake_clock)
        action = executor.execute(opp.id, strategy_id=strategy.id, actor=ACTOR)

        assert action.status is RecoveryStatus.SCHEDULED
        # Nothing reached the gateway; no attempt consumed; pre-execution.
        assert len(sim_gateway.orders) == 0
        assert action.attempts == 0
        assert action.executed_at is None
        # The delay counts from the policy decision at park time.
        assert action.decided_at is not None
        due = executor.scheduled_due_at(action)
        assert due is not None
        assert (due - action.decided_at).total_seconds() == 1800
        # Park transition is audited, with the due time in the details.
        trails = (
            db_session.query(models.AuditLog)
            .filter_by(entity_type="recovery_action", entity_id=action.id)
            .order_by(models.AuditLog.created_at, models.AuditLog.id)
            .all()
        )
        assert [r.action for r in trails] == [
            "recovery.action.proposed",
            "recovery.action.policy_evaluated",
            "recovery.action.scheduled",
        ]
        parked = trails[-1]
        assert parked.details["delay_seconds"] == 1800
        assert parked.details["due_at"] is not None
        assert parked.environment == action.environment
        # Opportunity shadow status follows the action.
        db_session.refresh(opp)
        assert opp.status is RecoveryStatus.SCHEDULED

    def test_immediate_strategy_still_fires_at_once(
        self, db_session, sim_gateway, fake_clock, make_opportunity, make_strategy,
        failed_payment,
    ):
        """Regression guard: constraints={} means no delay — fire immediately."""
        opp = make_opportunity(payment=failed_payment())
        strategy = make_strategy(opp, constraints={})

        action = RecoveryExecutor(db_session, sim_gateway, clock=fake_clock).execute(
            opp.id, strategy_id=strategy.id, actor=ACTOR
        )

        assert action.status is RecoveryStatus.VERIFYING
        assert len(sim_gateway.orders) == 1

    def test_parks_via_generated_delayed_strategy(
        self, db_session, sim_gateway, fake_clock, make_opportunity, make_diagnosis,
        failed_payment,
    ):
        """End-to-end through the real StrategyGenerator: the delayed-retry
        candidate of a timeout-class failure auto-parks when chosen."""
        opp = make_opportunity(payment=failed_payment())
        make_diagnosis(db_session.get(models.Incident, opp.incident_id), confidence=0.95)
        rows = StrategyGenerator(db_session).generate(opp)
        delayed = next(
            r for r in rows
            if r.action_type is ActionType.RETRY_PAYMENT and r.constraints.get("delay_seconds")
        )
        db_session.commit()

        action = RecoveryExecutor(db_session, sim_gateway, clock=fake_clock).execute(
            opp.id, strategy_id=delayed.id, actor=ACTOR
        )

        # 0.95 evidence x 0.90 delay_fit = 0.855 >= 0.85 floor -> ALLOWED -> parked.
        assert action.status is RecoveryStatus.SCHEDULED
        assert len(sim_gateway.orders) == 0

    def test_manual_execute_before_due_is_noop(
        self, db_session, sim_gateway, fake_clock, make_opportunity, make_strategy,
        failed_payment,
    ):
        opp = make_opportunity(payment=failed_payment())
        strategy = make_strategy(opp, constraints={"delay_seconds": 1800})
        executor = RecoveryExecutor(db_session, sim_gateway, clock=fake_clock)
        parked = executor.execute(opp.id, strategy_id=strategy.id, actor=ACTOR)

        again = executor.execute(opp.id, actor=ACTOR)

        assert again.id == parked.id
        assert again.status is RecoveryStatus.SCHEDULED
        assert len(sim_gateway.orders) == 0
        # No re-gate, no re-park: the audit trail is unchanged by the no-op.
        assert _audit_actions(db_session, parked.id) == [
            "recovery.action.proposed",
            "recovery.action.policy_evaluated",
            "recovery.action.scheduled",
        ]

    def test_cancel_scheduled_action(
        self, db_session, sim_gateway, fake_clock, make_opportunity, make_strategy,
        failed_payment,
    ):
        """SCHEDULED is pre-execution: a human can still cancel it."""
        opp = make_opportunity(payment=failed_payment())
        strategy = make_strategy(opp, constraints={"delay_seconds": 1800})
        executor = RecoveryExecutor(db_session, sim_gateway, clock=fake_clock)
        executor.execute(opp.id, strategy_id=strategy.id, actor=ACTOR)

        action = executor.cancel(opp.id, actor="human:ops", reason="customer paid offline")

        assert action.status is RecoveryStatus.CANCELLED
        assert len(sim_gateway.orders) == 0


class TestWorkerFiresWhenDue:
    def _park(self, db_session, sim_gateway, fake_clock, make_opportunity, make_strategy,
              failed_payment, delay=1800):
        opp = make_opportunity(payment=failed_payment())
        strategy = make_strategy(opp, constraints={"delay_seconds": delay})
        executor = RecoveryExecutor(db_session, sim_gateway, clock=fake_clock)
        action = executor.execute(opp.id, strategy_id=strategy.id, actor=ACTOR)
        db_session.commit()
        assert action.status is RecoveryStatus.SCHEDULED
        return opp, action

    def test_not_fired_before_due_fires_after_due(
        self, db_session, sim_gateway, make_worker, fake_clock, make_opportunity,
        make_strategy, failed_payment,
    ):
        opp, action = self._park(
            db_session, sim_gateway, fake_clock, make_opportunity, make_strategy, failed_payment
        )

        worker = make_worker()
        fake_clock.advance(seconds=1799)
        report = worker.tick()
        assert report.scheduled_seen == 1
        assert report.actions_fired == 0
        assert len(sim_gateway.orders) == 0

        fake_clock.advance(seconds=2)  # now past due (decided_at + 1800)
        report = worker.tick()
        assert report.scheduled_seen == 1
        assert report.actions_fired == 1
        assert len(sim_gateway.orders) == 1

        db_session.refresh(action)
        assert action.status is RecoveryStatus.VERIFYING
        assert action.attempts == 1
        assert action.executed_at is not None
        (order,) = sim_gateway.orders.values()
        # Same idempotency key minted at proposal time — one mutation, ever.
        assert order["receipt"] == action.gateway_request_id
        assert order["notes"]["requested_delay_seconds"] == "1800"

    def test_exactly_once_across_ticks(
        self, db_session, sim_gateway, make_worker, fake_clock, make_opportunity,
        make_strategy, failed_payment,
    ):
        opp, action = self._park(
            db_session, sim_gateway, fake_clock, make_opportunity, make_strategy, failed_payment
        )
        worker = make_worker()
        fake_clock.advance(seconds=1801)

        worker.tick()
        worker.tick()
        worker.tick()

        assert len(sim_gateway.orders) == 1
        db_session.refresh(action)
        assert action.status is RecoveryStatus.VERIFYING
        assert action.attempts == 1
        # Fire-time re-gate + transitions are audited; nothing double-fired.
        trails = _audit_actions(db_session, action.id)
        assert trails.count("recovery.action.executing") == 1
        assert trails.count("recovery.action.verifying") == 1

    def test_fire_time_regate_audits_worker_actor(
        self, db_session, sim_gateway, make_worker, fake_clock, make_opportunity,
        make_strategy, failed_payment,
    ):
        opp, action = self._park(
            db_session, sim_gateway, fake_clock, make_opportunity, make_strategy, failed_payment
        )
        fake_clock.advance(seconds=1801)
        make_worker().tick()

        rows = (
            db_session.query(models.AuditLog)
            .filter_by(entity_type="recovery_action", entity_id=action.id)
            .order_by(models.AuditLog.created_at, models.AuditLog.id)
            .all()
        )
        executing = next(r for r in rows if r.action == "recovery.action.executing")
        assert executing.actor == "system:worker"
        assert executing.environment == action.environment

    def test_cancelled_scheduled_action_never_fires(
        self, db_session, sim_gateway, make_worker, fake_clock, make_opportunity,
        make_strategy, failed_payment,
    ):
        opp, action = self._park(
            db_session, sim_gateway, fake_clock, make_opportunity, make_strategy, failed_payment
        )
        RecoveryExecutor(db_session, sim_gateway, clock=fake_clock).cancel(
            opp.id, actor="human:ops", reason="not needed"
        )
        db_session.commit()

        fake_clock.advance(seconds=3600)
        report = make_worker().tick()

        assert report.scheduled_seen == 0
        assert report.actions_fired == 0
        assert len(sim_gateway.orders) == 0

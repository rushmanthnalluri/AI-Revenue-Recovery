"""Notification outbox: the executor queues notify_customer contacts and the
worker delivers them via the NotificationSender port — with provenance,
backoff retry, and an honest FAILED after the attempt budget."""

import app.models as models
from app.ports import ActionType, NotificationStatus, RecoveryStatus
from app.services.recovery import RecoveryExecutor
from app.services.worker import NOTIFICATION_MAX_ATTEMPTS, NOTIFICATION_RETRY_BASE_SECONDS

from tests.worker.conftest import RecordingSender

ACTOR = "human:console"


def _enqueue(db_session, sim_gateway, fake_clock, make_opportunity, make_strategy,
             make_customer):
    customer = make_customer(name="Asha", email="asha@example.com")
    opp = make_opportunity(customer=customer)
    strategy = make_strategy(
        opp,
        action_type=ActionType.NOTIFY_CUSTOMER,
        constraints={"channel": "notification"},
    )
    executor = RecoveryExecutor(db_session, sim_gateway, clock=fake_clock)
    action = executor.execute(opp.id, strategy_id=strategy.id, actor=ACTOR)
    db_session.commit()
    return customer, action


def _outbox_rows(db_session):
    return list(
        db_session.query(models.NotificationOutbox)
        .order_by(models.NotificationOutbox.created_at, models.NotificationOutbox.id)
        .all()
    )


def _outbox_audits(db_session, row_id):
    return [
        r.action
        for r in db_session.query(models.AuditLog)
        .filter_by(entity_type="notification_outbox", entity_id=row_id)
        .order_by(models.AuditLog.created_at, models.AuditLog.id)
        .all()
    ]


class TestEnqueue:
    def test_notify_execute_queues_outbox_row(
        self, db_session, sim_gateway, fake_clock, make_opportunity, make_strategy,
        make_customer,
    ):
        customer, action = _enqueue(
            db_session, sim_gateway, fake_clock, make_opportunity, make_strategy, make_customer
        )

        # The action still verifies the usual way (customer payment webhook).
        assert action.status is RecoveryStatus.VERIFYING
        (row,) = _outbox_rows(db_session)
        assert row.status is NotificationStatus.PENDING
        assert row.attempts == 0
        assert row.action_id == action.id
        assert row.customer_id == customer.id
        assert row.channel == "notification"
        assert row.due_at == fake_clock()
        assert row.environment == action.environment
        assert row.payload["action_id"] == action.id
        assert row.payload["amount_paise"] == action.amount_paise
        assert row.payload["customer"]["email"] == "asha@example.com"
        # The gateway response references the outbox row (additive shape).
        assert action.gateway_response["outbox_id"] == row.id
        assert action.gateway_response["notified"] is True
        # Enqueue is audited with the environment stamped.
        assert _outbox_audits(db_session, row.id) == ["notification.queued"]
        entry = (
            db_session.query(models.AuditLog)
            .filter_by(entity_type="notification_outbox", entity_id=row.id)
            .one()
        )
        assert entry.environment == row.environment


class TestDelivery:
    def test_worker_delivers_pending_notification(
        self, db_session, sim_gateway, make_worker, fake_clock, make_opportunity,
        make_strategy, make_customer,
    ):
        _, action = _enqueue(
            db_session, sim_gateway, fake_clock, make_opportunity, make_strategy, make_customer
        )
        sender = RecordingSender()
        worker = make_worker(sender=sender)

        report = worker.tick()

        assert report.notifications_sent == 1
        assert len(sender.calls) == 1
        call = sender.calls[0]
        assert call["channel"] == "notification"
        assert call["payload"]["action_id"] == action.id
        (row,) = _outbox_rows(db_session)
        db_session.refresh(row)
        assert row.status is NotificationStatus.SENT
        assert row.attempts == 1
        assert row.sent_at == fake_clock()
        assert row.delivered_via == "recording"
        assert row.last_error is None
        assert _outbox_audits(db_session, row.id) == [
            "notification.queued",
            "notification.sent",
        ]

    def test_not_delivered_before_due(
        self, db_session, sim_gateway, make_worker, fake_clock, make_opportunity,
        make_strategy, make_customer,
    ):
        _enqueue(
            db_session, sim_gateway, fake_clock, make_opportunity, make_strategy, make_customer
        )
        (row,) = _outbox_rows(db_session)
        row.due_at = fake_clock()  # enqueue stamps due_at = now
        db_session.commit()
        # Wind the clock BACK: the row is not due yet.
        fake_clock.advance(seconds=-60)
        sender = RecordingSender()

        make_worker(sender=sender).tick()

        assert len(sender.calls) == 0
        db_session.refresh(row)
        assert row.status is NotificationStatus.PENDING

    def test_failure_retries_with_backoff_then_succeeds(
        self, db_session, sim_gateway, make_worker, fake_clock, make_opportunity,
        make_strategy, make_customer,
    ):
        _enqueue(
            db_session, sim_gateway, fake_clock, make_opportunity, make_strategy, make_customer
        )
        sender = RecordingSender(fail_first=1)
        worker = make_worker(sender=sender)

        # First attempt fails: row stays PENDING, due_at pushed out by backoff.
        worker.tick()
        (row,) = _outbox_rows(db_session)
        db_session.refresh(row)
        assert row.status is NotificationStatus.PENDING
        assert row.attempts == 1
        assert "NotificationDeliveryError" in (row.last_error or "")
        expected_due = fake_clock()
        from datetime import timedelta

        assert row.due_at == expected_due + timedelta(
            seconds=NOTIFICATION_RETRY_BASE_SECONDS
        )
        assert _outbox_audits(db_session, row.id) == [
            "notification.queued",
            "notification.retry_scheduled",
        ]

        # A tick before the backoff elapses does NOT retry.
        fake_clock.advance(seconds=NOTIFICATION_RETRY_BASE_SECONDS - 1)
        worker.tick()
        assert len(sender.calls) == 1

        # Once due, the retry fires and succeeds.
        fake_clock.advance(seconds=2)
        worker.tick()
        assert len(sender.calls) == 2
        db_session.refresh(row)
        assert row.status is NotificationStatus.SENT
        assert row.attempts == 2
        assert row.last_error is None

    def test_permanent_failure_marks_failed_after_max_attempts(
        self, db_session, sim_gateway, make_worker, fake_clock, make_opportunity,
        make_strategy, make_customer,
    ):
        _enqueue(
            db_session, sim_gateway, fake_clock, make_opportunity, make_strategy, make_customer
        )
        sender = RecordingSender(always_fail=True)
        worker = make_worker(sender=sender)

        for _ in range(NOTIFICATION_MAX_ATTEMPTS):
            worker.tick()
            fake_clock.advance(seconds=NOTIFICATION_RETRY_BASE_SECONDS * 10)

        (row,) = _outbox_rows(db_session)
        db_session.refresh(row)
        assert row.status is NotificationStatus.FAILED
        assert row.attempts == NOTIFICATION_MAX_ATTEMPTS
        assert row.last_error
        assert _outbox_audits(db_session, row.id)[-1] == "notification.failed"

        # FAILED is terminal for the worker: no further attempts.
        worker.tick()
        assert len(sender.calls) == NOTIFICATION_MAX_ATTEMPTS

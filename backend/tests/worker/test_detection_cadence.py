"""Scheduled detection: the worker runs one REAL_TEST detection pass on the
first tick after startup and then on the WORKER_DETECTION_SECONDS cadence —
failure-isolated, audited, and never touching the research environment
(research stays on-demand via the demo/eval surfaces).
"""

import sqlalchemy as sa
from sqlalchemy.orm import Session

import app.models as models
from app.db import utcnow
from app.services.detection import DetectionRunResult
from app.services.worker import Worker


def _spy_detection(calls):
    def _run(db, *, actor):
        calls.append({"actor": actor, "db": db})
        return DetectionRunResult(run_id="det_test", status="completed", started_at=utcnow())

    return _run


def _seed_terminal_payment(db_session: Session, *, source_type: str, external_id: str) -> None:
    """One captured payment + its terminal event under the given provenance."""
    merchant = models.Merchant(name="Detection Cadence Merchant")
    db_session.add(merchant)
    db_session.flush()
    payment = models.Payment(
        merchant_id=merchant.id,
        amount_paise=50_000,
        status="captured",
        captured=True,
        source_type=source_type,
        source_system="razorpay" if source_type.startswith("razorpay") else "pulserecover-simulator",
        external_id=external_id,
        gateway_payment_id=external_id,
    )
    db_session.add(payment)
    db_session.flush()
    db_session.add(
        models.PaymentEvent(
            payment_id=payment.id,
            event_type="payment.captured",
            to_status="captured",
            source="sync" if source_type.startswith("razorpay") else "simulator",
            occurred_at=utcnow(),
            source_type=source_type,
            external_id=external_id,
        )
    )
    db_session.commit()


class TestDetectionCadence:
    def test_first_tick_detects_then_cadence_gates(
        self, db_session, sim_gateway, session_factory, fake_clock
    ):
        calls = []

        worker = Worker(
            session_factory,
            sim_gateway,
            clock=fake_clock,
            detection_seconds=300.0,
            reconcile_fn=lambda db, gateway, *, actor: None,
            detection_fn=_spy_detection(calls),
        )

        # First tick after startup always detects (the stream accrues while
        # the process is down).
        report = worker.tick()
        assert len(calls) == 1
        assert report.detected is True
        assert report.detection_result is not None

        # Inside the cadence window: no pass.
        fake_clock.advance(seconds=299)
        report = worker.tick()
        assert len(calls) == 1
        assert report.detected is False
        assert report.detection_result is None

        # Cadence reached: detect again.
        fake_clock.advance(seconds=1)
        report = worker.tick()
        assert len(calls) == 2
        assert report.detected is True

    def test_detection_runs_with_worker_actor_and_own_session(
        self, db_session, sim_gateway, session_factory, fake_clock
    ):
        calls = []

        worker = Worker(
            session_factory,
            sim_gateway,
            clock=fake_clock,
            detection_seconds=300.0,
            reconcile_fn=lambda db, gateway, *, actor: None,
            detection_fn=_spy_detection(calls),
        )
        worker.tick()

        assert calls[0]["actor"] == "system:worker"
        assert calls[0]["db"] is not db_session  # the worker's own session

    def test_failed_pass_retries_next_tick(
        self, db_session, sim_gateway, session_factory, fake_clock
    ):
        attempts = []

        def _failing(db, *, actor):
            attempts.append(1)
            raise RuntimeError("detector exploded")

        worker = Worker(
            session_factory,
            sim_gateway,
            clock=fake_clock,
            detection_seconds=300.0,
            reconcile_fn=lambda db, gateway, *, actor: None,
            detection_fn=_failing,
        )

        report = worker.tick()
        assert report.detected is False
        assert report.errors and report.errors[0].startswith("detection: RuntimeError")

        # The cadence did NOT advance on failure: the very next tick retries.
        report = worker.tick()
        assert len(attempts) == 2
        assert report.detected is False

    def test_default_unit_scopes_to_real_test_and_ignores_research(
        self, db_session, sim_gateway, session_factory, fake_clock
    ):
        """The real default unit (`run_real_test_detection`) runs against a
        DB whose only terminal events are RESEARCH (simulator) rows: the pass
        must see nothing (real_test anchor is None), persist nothing, and
        leave the research rows untouched."""
        _seed_terminal_payment(db_session, source_type="simulator", external_id="pay_Sim01")
        events_before = int(
            db_session.scalar(sa.select(sa.func.count()).select_from(models.PaymentEvent)) or 0
        )

        worker = Worker(
            session_factory,
            sim_gateway,
            clock=fake_clock,
            detection_seconds=300.0,
            reconcile_fn=lambda db, gateway, *, actor: None,
            # detection_fn omitted: the real default unit runs.
        )
        report = worker.tick()

        assert report.detected is True
        result = report.detection_result
        assert result is not None and result.status == "completed"
        # Research events exist but the pass anchors on real_test only.
        assert result.detail == "no terminal payment events in scope; nothing to detect"
        assert (
            int(
                db_session.scalar(sa.select(sa.func.count()).select_from(models.Incident)) or 0
            )
            == 0
        )
        # Detection never writes to the event stream (research rows intact).
        assert (
            int(
                db_session.scalar(sa.select(sa.func.count()).select_from(models.PaymentEvent))
                or 0
            )
            == events_before
        )
        # The run itself joins the audit trail, stamped real_test.
        entry = db_session.scalar(
            sa.select(models.AuditLog).where(models.AuditLog.action == "detection.run")
        )
        assert entry is not None
        assert entry.actor == "system:worker"
        assert entry.environment == "real_test"
        assert entry.details["trigger"] == "worker"

    def test_default_unit_consumes_real_test_events(
        self, db_session, sim_gateway, session_factory, fake_clock
    ):
        """A real_test terminal event (the shape sync derives) is seen by the
        worker's detection pass."""
        _seed_terminal_payment(
            db_session, source_type="razorpay_test", external_id="pay_Real01"
        )

        worker = Worker(
            session_factory,
            sim_gateway,
            clock=fake_clock,
            detection_seconds=300.0,
            reconcile_fn=lambda db, gateway, *, actor: None,
        )
        report = worker.tick()

        assert report.detected is True
        result = report.detection_result
        assert result is not None and result.status == "completed"
        assert result.detail is not None and "outcomes=1" in result.detail

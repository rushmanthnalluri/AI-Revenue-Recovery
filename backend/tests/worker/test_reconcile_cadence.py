"""Scheduled reconciliation: the worker runs the ADR 0011 sweep on the first
tick after startup and then on the configured cadence — and only then."""

from app.services.recovery.reconcile import ReconcileReport


def _spy_reconcile(calls):
    def _run(db, gateway, *, actor):
        calls.append({"actor": actor, "gateway": gateway, "db": db})
        return ReconcileReport(sweep_id="rcn_test")

    return _run


class TestReconcileCadence:
    def test_first_tick_reconciles_then_cadence_gates(
        self, db_session, sim_gateway, session_factory, fake_clock,
    ):
        calls = []
        from app.services.worker import Worker

        worker = Worker(
            session_factory,
            sim_gateway,
            clock=fake_clock,
            reconcile_seconds=900.0,
            reconcile_fn=_spy_reconcile(calls),
        )

        # First tick after startup always sweeps (drift accumulates while down).
        report = worker.tick()
        assert len(calls) == 1
        assert report.reconciled is True
        assert report.reconcile_report is not None
        assert report.reconcile_report.sweep_id == "rcn_test"

        # Inside the cadence window: no sweep.
        fake_clock.advance(seconds=899)
        report = worker.tick()
        assert len(calls) == 1
        assert report.reconciled is False
        assert report.reconcile_report is None

        # Cadence reached: sweep again.
        fake_clock.advance(seconds=1)
        report = worker.tick()
        assert len(calls) == 2
        assert report.reconciled is True

    def test_reconcile_runs_with_worker_actor_and_gateway(
        self, db_session, sim_gateway, session_factory, fake_clock,
    ):
        calls = []
        from app.services.worker import Worker

        worker = Worker(
            session_factory,
            sim_gateway,
            clock=fake_clock,
            reconcile_seconds=900.0,
            reconcile_fn=_spy_reconcile(calls),
        )
        worker.tick()

        assert calls[0]["actor"] == "system:worker"
        assert calls[0]["gateway"] is sim_gateway
        assert calls[0]["db"] is not db_session  # the worker's own session

    def test_failed_sweep_retries_next_tick(
        self, db_session, sim_gateway, session_factory, fake_clock,
    ):
        from app.services.worker import Worker

        attempts = []

        def _failing(db, gateway, *, actor):
            attempts.append(1)
            raise RuntimeError("gateway unreachable")

        worker = Worker(
            session_factory,
            sim_gateway,
            clock=fake_clock,
            reconcile_seconds=900.0,
            reconcile_fn=_failing,
        )

        report = worker.tick()
        assert report.reconciled is False
        assert report.errors and report.errors[0].startswith("reconcile: RuntimeError")

        # The cadence did NOT advance on failure: the very next tick retries.
        report = worker.tick()
        assert len(attempts) == 2
        assert report.reconciled is False

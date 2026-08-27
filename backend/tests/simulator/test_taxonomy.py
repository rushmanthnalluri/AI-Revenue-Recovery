"""Failure telemetry mirrors the Razorpay taxonomy: every failed payment
carries a valid (error_code, error_source, error_step, error_reason) tuple for
its method, and successful payments carry no error fields."""

import sqlalchemy as sa

from app.models import Payment, PaymentEvent
from app.simulator import run_simulation
from app.simulator.taxonomy import FAILURES, METHOD_FAILURE_WEIGHTS
from tests.simulator.conftest import fresh_session, small_config


def test_failed_payments_follow_taxonomy():
    session = fresh_session()
    try:
        run_simulation(small_config(), session)

        # every failed payment row: error fields set + taxonomy-valid
        rows = session.execute(
            sa.select(Payment.method, Payment.error_code, Payment.error_source,
                      Payment.meta)
            .where(Payment.status == "failed")
        ).all()
        assert len(rows) > 100
        for method, code, source, meta in rows:
            reason = meta.get("error_reason")
            step = meta.get("error_step")
            assert reason in FAILURES, f"unknown reason {reason}"
            allowed = {r for r, _ in METHOD_FAILURE_WEIGHTS[method]} | {
                # incident-forced reasons may cross methods (e.g. a gateway
                # degradation forces gateway_technical_error on any method)
                "gateway_technical_error", "payment_timed_out", "bank_downtime",
                "bank_technical_error", "insufficient_fund",
                "transaction_limit_exceeded", "payment_declined",
            }
            assert reason in allowed, f"reason {reason} invalid for {method}"
            spec = FAILURES[reason]
            assert code == spec.error_code
            assert source == spec.error_source
            assert step == spec.error_step

        # failed payment events carry the full Razorpay-style telemetry set
        ev_rows = session.execute(
            sa.select(PaymentEvent.payload).where(
                PaymentEvent.event_type == "payment.failed"
            ).limit(500)
        ).all()
        assert ev_rows
        for (payload,) in ev_rows:
            for key in ("error_code", "error_description", "error_source",
                        "error_step", "error_reason"):
                assert payload[key], f"missing {key} in failed payload"
            spec = FAILURES[payload["error_reason"]]
            assert payload["error_code"] == spec.error_code
            assert payload["error_source"] == spec.error_source
            assert payload["error_step"] == spec.error_step

        # captured payments with error fields are exactly the late captures
        # (payment.failed followed by payment.captured — a documented
        # Razorpay quirk); each must have a failed event proving it
        bad_ids = [
            r[0]
            for r in session.execute(
                sa.select(Payment.id).where(
                    Payment.status == "captured", Payment.error_code.is_not(None)
                )
            ).all()
        ]
        total_captured = session.scalar(
            sa.select(sa.func.count()).select_from(Payment).where(Payment.status == "captured")
        )
        assert len(bad_ids) / total_captured <= 0.03
        for pid in bad_ids:
            n_failed_ev = session.scalar(
                sa.select(sa.func.count()).select_from(PaymentEvent).where(
                    PaymentEvent.payment_id == pid,
                    PaymentEvent.event_type == "payment.failed",
                )
            )
            assert n_failed_ev == 1, f"{pid} captured with errors but no failed event"
    finally:
        session.close()


def test_event_stream_shape():
    """Every payment's event chain is coherent: starts with payment.created,
    terminal states match the payment row, occurred_at is non-decreasing."""
    session = fresh_session()
    try:
        run_simulation(small_config(), session)
        rows = session.execute(
            sa.select(Payment.id, Payment.status, PaymentEvent.event_type,
                      PaymentEvent.occurred_at)
            .join(PaymentEvent, PaymentEvent.payment_id == Payment.id)
            .order_by(Payment.id, PaymentEvent.occurred_at)
            .limit(20_000)
        ).all()
        by_payment: dict[str, list] = {}
        statuses: dict[str, str] = {}
        for pid, status, etype, occurred in rows:
            by_payment.setdefault(pid, []).append((etype, occurred))
            statuses[pid] = status
        for pid, events in by_payment.items():
            assert events[0][0] == "payment.created"
            occurred = [o for _, o in events]
            assert occurred == sorted(occurred), f"{pid} events out of order"
            terminal = events[-1][0]
            status = statuses[pid]
            if status == "captured":
                assert terminal == "payment.captured"
            elif status == "failed":
                assert terminal == "payment.failed"
            else:
                assert terminal == "payment.created" and len(events) == 1
    finally:
        session.close()

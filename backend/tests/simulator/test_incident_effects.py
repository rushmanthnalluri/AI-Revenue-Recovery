"""Incident windows measurably shift their target metrics. Each test runs the
same seed with the default incident schedule vs. a quiet run, and compares
in-window vs out-of-window rates.

JSON fields (Payment.meta, PaymentEvent.payload) are extracted in Python
rather than via dialect JSON operators — portable across SQLite/Postgres.
"""

from datetime import datetime, timedelta

import sqlalchemy as sa

from app.models import Payment, PaymentEvent, SimulatorGroundTruth
from app.simulator import run_simulation
from app.simulator.config import IncidentKind
from tests.simulator.conftest import FIXED_END, fresh_session, small_config

WINDOW_START = FIXED_END - timedelta(days=10)


def _incident_windows(session):
    out = {}
    rows = session.execute(
        sa.select(SimulatorGroundTruth).where(
            SimulatorGroundTruth.entity_type == "incident"
        )
    ).scalars()
    for g in rows:
        out[g.truth["kind"]] = (datetime.fromisoformat(g.truth["start"]),
                                datetime.fromisoformat(g.truth["end"]))
    return out


def _fail_rate(session, start, end, method=None, subscription=None):
    """Failure rate over attempted (non-abandoned) payments in [start, end).
    subscription=None → all payments; True/False → sub/non-sub only."""
    q = sa.select(Payment.status, Payment.meta).where(
        Payment.created_at >= start, Payment.created_at < end,
        Payment.status != "created",
    )
    if method:
        q = q.where(Payment.method == method)
    n = failed = 0
    for status, meta in session.execute(q).all():
        if subscription is not None and ("subscription_id" in meta) != subscription:
            continue
        n += 1
        failed += status == "failed"
    return (failed / n if n else 0.0), n


def _failed_reasons(session, start, end, method=None, methods=None):
    q = sa.select(Payment.meta).where(
        Payment.created_at >= start, Payment.created_at < end,
        Payment.status == "failed",
    )
    if method:
        q = q.where(Payment.method == method)
    if methods:
        q = q.where(Payment.method.in_(methods))
    return [meta.get("error_reason") for (meta,) in session.execute(q).all()]


def test_method_outage_craters_upi_success_rate():
    session = fresh_session()
    try:
        run_simulation(small_config(), session)
        s, e = _incident_windows(session)[IncidentKind.METHOD_OUTAGE.value]

        in_rate, n_in = _fail_rate(session, s, e, method="upi")
        out_rate, _ = _fail_rate(session, WINDOW_START, s, method="upi")
        assert n_in >= 15, f"too few in-window UPI payments: {n_in}"
        assert in_rate >= 0.70, f"in-window UPI fail rate {in_rate:.2f}"
        assert out_rate <= 0.30, f"baseline UPI fail rate {out_rate:.2f}"
        assert in_rate - out_rate >= 0.45

        # failure reasons inside the window are downtime-flavored
        reasons = _failed_reasons(session, s, e, method="upi")
        downtime = sum(1 for r in reasons if r in ("bank_downtime", "bank_technical_error"))
        assert downtime / len(reasons) >= 0.6
    finally:
        session.close()


def test_route_latency_spikes_capture_latency():
    session = fresh_session()
    try:
        run_simulation(small_config(), session)
        s, e = _incident_windows(session)[IncidentKind.ROUTE_LATENCY.value]

        def latencies(start, end):
            q = (
                sa.select(PaymentEvent.payload, Payment.meta)
                .join(Payment, Payment.id == PaymentEvent.payment_id)
                .where(
                    PaymentEvent.event_type == "payment.captured",
                    PaymentEvent.occurred_at >= start,
                    PaymentEvent.occurred_at < end,
                )
            )
            return [
                float(payload["latency_ms"])
                for payload, meta in session.execute(q).all()
                if meta.get("route") == "pg_primary" and "latency_ms" in payload
            ]

        in_lat = latencies(s, e)
        out_lat = latencies(s - timedelta(days=5), s)
        assert len(in_lat) >= 15 and len(out_lat) >= 100
        mean_in = sum(in_lat) / len(in_lat)
        mean_out = sum(out_lat) / len(out_lat)
        assert mean_in >= 4 * mean_out, f"{mean_in:.0f} vs {mean_out:.0f}"
    finally:
        session.close()


def test_insufficient_funds_wave_shifts_reason_mix():
    session = fresh_session()
    try:
        run_simulation(small_config(), session)
        s, e = _incident_windows(session)[
            IncidentKind.CUSTOMER_INSUFFICIENT_FUNDS_WAVE.value
        ]

        def insf_share(start, end):
            reasons = _failed_reasons(session, start, end, methods=("card", "upi"))
            if not reasons:
                return 0.0, 0
            share = sum(1 for r in reasons if r == "insufficient_fund") / len(reasons)
            return share, len(reasons)

        in_share, n_in = insf_share(s, e)
        out_share, _ = insf_share(s - timedelta(days=6), s)
        assert n_in >= 30
        assert in_share >= 0.55, f"in-window insufficient_fund share {in_share:.2f}"
        assert in_share >= 1.6 * max(out_share, 1e-9)
    finally:
        session.close()


def test_abandonment_spike_creates_unattempted_payments():
    session = fresh_session()
    try:
        run_simulation(small_config(), session)
        s, e = _incident_windows(session)[
            IncidentKind.CHECKOUT_ABANDONMENT_SPIKE.value
        ]

        def created_rate(start, end):
            q = sa.select(Payment.status, Payment.meta).where(
                Payment.created_at >= start, Payment.created_at < end,
            )
            rows = [(st, m) for st, m in session.execute(q).all()
                    if "subscription_id" not in m]
            if not rows:
                return 0.0, 0
            created = sum(1 for st, _ in rows if st == "created")
            return created / len(rows), len(rows)

        in_rate, n_in = created_rate(s, e)
        out_rate, _ = created_rate(s - timedelta(days=5), s)
        assert n_in >= 30
        assert in_rate >= 0.35, f"in-window created rate {in_rate:.2f}"
        assert in_rate >= 5 * max(out_rate, 1e-9)
        # abandoned payments have exactly one event (payment.created)
        n_events = session.scalar(
            sa.select(sa.func.count())
            .select_from(PaymentEvent)
            .join(Payment, Payment.id == PaymentEvent.payment_id)
            .where(Payment.created_at >= s, Payment.created_at < e,
                   Payment.status == "created")
        )
        n_created = session.scalar(
            sa.select(sa.func.count()).select_from(Payment).where(
                Payment.created_at >= s, Payment.created_at < e,
                Payment.status == "created")
        )
        assert n_events == n_created
    finally:
        session.close()


def test_subscription_failure_spike_hits_recurring_charges():
    session = fresh_session()
    try:
        # more customers ⇒ more subs ⇒ a solid in-window sample
        run_simulation(
            small_config(customers=2_000, days=30, target_events=8_000), session
        )
        s, e = _incident_windows(session)[
            IncidentKind.SUBSCRIPTION_FAILURE_SPIKE.value
        ]

        in_rate, n_in = _fail_rate(session, s, e, subscription=True)
        out_rate, _ = _fail_rate(session, s - timedelta(days=28), s, subscription=True)
        assert n_in >= 6, f"in-window subscription charges: {n_in}"
        assert in_rate >= 0.40, f"in-window sub fail rate {in_rate:.2f}"
        # out-of-window baseline is inflated by dunning retry attempts (which
        # only exist after a failure), so the ratio threshold stays modest;
        # the absolute gap (+20pp) is the real signal
        assert in_rate >= 1.9 * max(out_rate, 1e-9)
        assert in_rate - out_rate >= 0.20
    finally:
        session.close()


def test_quiet_run_has_no_metric_shifts():
    session = fresh_session()
    try:
        result = run_simulation(small_config(scenario="quiet", incidents=()), session)
        start = datetime.fromisoformat(result.stats["window"]["start"])
        mid = start + timedelta(days=5)
        r1, n1 = _fail_rate(session, start, mid, method="upi")
        r2, n2 = _fail_rate(session, mid, mid + timedelta(days=5), method="upi")
        assert n1 > 100 and n2 > 100
        assert abs(r1 - r2) < 0.08
    finally:
        session.close()

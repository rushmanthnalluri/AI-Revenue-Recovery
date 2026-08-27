"""Determinism: same seed + config ⇒ identical dataset; different seed differs."""

import sqlalchemy as sa

from app.models import (
    Order,
    Payment,
    PaymentEvent,
    SimulatorGroundTruth,
    Subscription,
)
from app.simulator import run_simulation
from tests.simulator.conftest import fresh_session, small_config


def _fingerprint(session) -> dict:
    """Aggregate fingerprint of everything the simulator wrote."""
    def counts(model):
        return session.scalar(sa.select(sa.func.count()).select_from(model))

    pay_rows = session.execute(
        sa.select(Payment.id, Payment.status, Payment.method, Payment.amount_paise,
                  Payment.error_code, Payment.error_source)
        .order_by(Payment.id)
    ).all()
    ev_counts = session.execute(
        sa.select(PaymentEvent.event_type, sa.func.count()).group_by(PaymentEvent.event_type)
    ).all()
    gt_rows = session.execute(
        sa.select(SimulatorGroundTruth.entity_type, SimulatorGroundTruth.entity_id,
                  SimulatorGroundTruth.truth)
        .order_by(SimulatorGroundTruth.entity_type, SimulatorGroundTruth.entity_id)
    ).all()
    sub_rows = session.execute(
        sa.select(Subscription.id, Subscription.status, Subscription.retry_count)
        .order_by(Subscription.id)
    ).all()
    # truth JSON may contain lists; normalize for hashing
    gt_norm = [
        (t, e, str(sorted(truth.items()))) for t, e, truth in gt_rows
    ]
    return {
        "counts": {
            m.__name__: counts(m)
            for m in (Order, Payment, PaymentEvent, Subscription, SimulatorGroundTruth)
        },
        "payments": [tuple(r) for r in pay_rows],
        "events": sorted((t, c) for t, c in ev_counts),
        "ground_truth": gt_norm,
        "subscriptions": [tuple(r) for r in sub_rows],
    }


def test_same_seed_same_dataset():
    s1, s2 = fresh_session(), fresh_session()
    try:
        cfg = small_config()
        run_simulation(cfg, s1)
        run_simulation(cfg, s2)
        f1, f2 = _fingerprint(s1), _fingerprint(s2)
        assert f1["counts"] == f2["counts"]
        assert f1["payments"] == f2["payments"]
        assert f1["events"] == f2["events"]
        assert f1["ground_truth"] == f2["ground_truth"]
        assert f1["subscriptions"] == f2["subscriptions"]
    finally:
        s1.close()
        s2.close()


def test_different_seed_differs():
    s1, s2 = fresh_session(), fresh_session()
    try:
        run_simulation(small_config(seed=7), s1)
        run_simulation(small_config(seed=99), s2)
        f1, f2 = _fingerprint(s1), _fingerprint(s2)
        # statistically impossible for the full payment rows to match
        assert f1["payments"] != f2["payments"]
    finally:
        s1.close()
        s2.close()

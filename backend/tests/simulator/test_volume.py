"""Volume: the default config meets the assignment scale bar —
1 merchant, 2k-5k customers, ~30 days, 60k+ payment_events."""

import sqlalchemy as sa

from app.models import Customer, Merchant, Payment, PaymentEvent, Subscription
from app.simulator import SimulatorConfig, run_simulation
from tests.simulator.conftest import FIXED_END, fresh_session


def test_default_scale_volume():
    session = fresh_session()
    try:
        cfg = SimulatorConfig(end_date=FIXED_END)  # defaults: 30d, 65k target
        result = run_simulation(cfg, session)
        rows = result.stats["rows"]
        assert rows["merchants"] == 1
        assert 2_000 <= rows["customers"] <= 5_000
        assert rows["subscriptions"] >= 30
        n_events = session.scalar(sa.select(sa.func.count()).select_from(PaymentEvent))
        n_payments = session.scalar(sa.select(sa.func.count()).select_from(Payment))
        assert n_events == rows["payment_events"]
        assert n_payments == rows["payments"]
        assert n_events >= 60_000, f"volume bar missed: {n_events}"
        assert n_payments >= 25_000
        # sanity: stats self-consistency
        assert session.scalar(sa.select(sa.func.count()).select_from(Customer)) == rows["customers"]
        assert session.scalar(sa.select(sa.func.count()).select_from(Subscription)) == rows["subscriptions"]
        assert session.scalar(sa.select(sa.func.count()).select_from(Merchant)) == 1
    finally:
        session.close()

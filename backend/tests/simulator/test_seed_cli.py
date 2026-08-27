"""seed.py semantics: identical config ⇒ skip; --force deletes + regenerates;
delete_simulator_run removes every generated row."""

import sqlalchemy as sa

from app.models import (
    Customer,
    Merchant,
    Order,
    Payment,
    PaymentEvent,
    SimulatorGroundTruth,
    SimulatorRun,
    Subscription,
)
from app.simulator.cli import run_idempotent
from app.simulator.engine import delete_simulator_run
from tests.simulator.conftest import fresh_session, small_config


def _total_rows(session) -> int:
    total = 0
    for m in (Merchant, Customer, Subscription, Order, Payment, PaymentEvent,
              SimulatorGroundTruth, SimulatorRun):
        total += session.scalar(sa.select(sa.func.count()).select_from(m))
    return total


def test_idempotent_skip_and_force():
    session = fresh_session()
    try:
        cfg = small_config()
        r1 = run_idempotent(session, cfg, force=False)
        assert not r1.skipped
        rows_after_first = _total_rows(session)

        # second identical run: skipped, no new rows
        r2 = run_idempotent(session, cfg, force=False)
        assert r2.skipped
        assert r2.run_id == r1.run_id
        assert _total_rows(session) == rows_after_first

        # force: same row counts afterwards (same config ⇒ same dataset)
        r3 = run_idempotent(session, cfg, force=True)
        assert not r3.skipped
        assert _total_rows(session) == rows_after_first
    finally:
        session.close()


def test_delete_simulator_run_cascades():
    session = fresh_session()
    try:
        cfg = small_config()
        result = run_idempotent(session, cfg, force=False)
        assert _total_rows(session) > 0
        counts = delete_simulator_run(session, result.run_id)
        assert counts["payments"] > 0
        assert counts["payment_events"] > 0
        assert counts["simulator_ground_truth"] > 0
        assert _total_rows(session) == 0
        # deleting again is a no-op
        assert delete_simulator_run(session, result.run_id) == {}
    finally:
        session.close()


def test_parse_incidents_selection():
    from app.simulator.cli import parse_incidents

    assert len(parse_incidents("default")) == 6
    assert parse_incidents("none") == ()
    only = parse_incidents("method_outage,customer_insufficient_funds_wave")
    assert {i.kind.value for i in only} == {
        "method_outage",
        "customer_insufficient_funds_wave",
    }

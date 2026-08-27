"""Ground truth internal consistency: what the simulator claims it injected
must line up exactly with the payment data on disk."""

from datetime import datetime, timezone

import sqlalchemy as sa

from app.models import Payment, SimulatorGroundTruth, SimulatorRun, Subscription
from app.simulator import run_simulation
from tests.simulator.conftest import fresh_session, small_config


def _load(session):
    run = session.execute(sa.select(SimulatorRun)).scalar_one()
    gts = session.execute(sa.select(SimulatorGroundTruth)).scalars().all()
    incidents = [g for g in gts if g.entity_type == "incident"]
    pay_gts = [g for g in gts if g.entity_type == "payment"]
    sub_gts = [g for g in gts if g.entity_type == "subscription"]
    payments = {p.id: p for p in session.execute(sa.select(Payment)).scalars()}
    return run, incidents, pay_gts, sub_gts, payments


def test_ground_truth_consistency():
    session = fresh_session()
    try:
        run_simulation(small_config(), session)
        run, incidents, pay_gts, sub_gts, payments = _load(session)
        assert len(incidents) == len(small_config().incidents) == 6

        win_start = datetime.fromisoformat(run.stats["window"]["start"])
        win_end = datetime.fromisoformat(run.stats["window"]["end"])
        incident_ids = set()
        affected_union = set()

        for inc in incidents:
            t = inc.truth
            incident_ids.add(inc.entity_id)
            start = datetime.fromisoformat(t["start"])
            end = datetime.fromisoformat(t["end"])
            assert win_start <= start < end <= win_end, inc.entity_id
            ids = t["affected_payment_ids"]
            assert t["affected_count"] == len(ids)
            assert len(ids) == len(set(ids)), "duplicate affected ids"
            assert t["affected_count"] > 0, f"{t['kind']} affected nothing"
            for pid in ids:
                assert pid in payments, f"affected payment {pid} missing"
                ts = payments[pid].created_at
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                assert start <= ts < end, f"{pid} outside incident window"
            assert t["expected_root_cause"]
            assert t["expected_signature"]["metric"]
            affected_union.update(ids)

        # every affected payment has a payment-level ground truth row and
        # vice versa
        pay_gt_ids = {g.entity_id for g in pay_gts}
        assert pay_gt_ids == affected_union

        for g in pay_gts:
            t = g.truth
            p = payments[g.entity_id]
            assert set(t["incident_ids"]) <= incident_ids
            assert t["final_outcome"] == p.status
            assert t["injected"] == (t["natural_outcome"] != t["final_outcome"])
            if t["final_outcome"] == "failed":
                assert t["error_reason"]
                assert p.error_code is not None

        for g in sub_gts:
            sub = session.get(Subscription, g.entity_id)
            assert sub is not None
            assert sub.status == "halted"
            assert set(g.truth["incident_ids"]) <= incident_ids
    finally:
        session.close()


def test_quiet_run_has_no_ground_truth():
    session = fresh_session()
    try:
        run_simulation(small_config(scenario="quiet", incidents=()), session)
        n = session.scalar(sa.select(sa.func.count()).select_from(SimulatorGroundTruth))
        assert n == 0
    finally:
        session.close()

"""Run-metric completeness (claim-matrix 16.1): the per-type opportunity
split is persisted in the stored run metrics, so the failed-payment vs
stuck-checkout (vs subscription) breakdown is machine-checkable instead of
derived during run analysis. Additive key: older runs simply lack it.

Runs at the smallest scale whose event density (~83 events/hour) lets the
scheduled detection passes persist an incident — below that the harness is
plumbing-scale and builds no opportunities at all.
"""

from app.services.evaluation import EvaluationRunner

# ~83 events/hour: the density detection needs (5+ per 5-min bucket).
_SCALE = {"days": 5, "events": 10_000, "customers": 500}


def test_opportunity_types_breakdown_is_persisted(db_session):
    run = EvaluationRunner(db_session).run(
        name="otypes",
        scenario="upi_outage_demo",
        seed=11,
        holdout_fraction=0.25,
        **_SCALE,
    )
    assert run.status == "completed"

    pulsecover = run.metrics["arms"]["pulsecover"]
    types = pulsecover["opportunity_types"]
    assert isinstance(types, dict) and types
    # The split is exhaustive: parts sum to the persisted total.
    assert sum(types.values()) == pulsecover["opportunities_count"]
    # Only builder-produced types, and every value is a positive count.
    assert set(types) <= {
        "failed_payment_retry",
        "dropped_checkout",
        "stuck_checkout_payment",
        "subscription_halted",
    }
    assert all(isinstance(v, int) and v > 0 for v in types.values())
    # The canonical split's lanes are present (claim-matrix 4.7).
    assert "failed_payment_retry" in types
    assert "stuck_checkout_payment" in types

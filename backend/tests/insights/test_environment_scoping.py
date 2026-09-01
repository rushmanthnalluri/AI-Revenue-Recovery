"""Environment scoping for incident_insights (the real_test/research boundary).

The incident window AND the fleet benchmark must read only the incident's own
environment's payments — a flood of the other environment's failures in the
same window must not change the counts (docs/data-provenance.md).
"""

from datetime import datetime

import app.models as models
from app.services.insights.service import InsightsService

from tests.insights.conftest import (
    BASELINE_START,
    TS_BASELINE,
    TS_WINDOW,
    WINDOW_END,
    WINDOW_START,
)


def _add_outcome(
    db_session,
    merchant,
    *,
    ts: datetime,
    source_type: str,
    success: bool,
    method: str = "upi",
) -> models.Payment:
    """One payment with a terminal event at ``ts`` and explicit provenance."""
    status = "captured" if success else "failed"
    payment = models.Payment(
        merchant_id=merchant.id,
        amount_paise=10000,
        status=status,
        method=method,
        source_type=source_type,
        meta={} if success else {"error_reason": "payment_timed_out"},
        gateway_created_at=ts,
    )
    db_session.add(payment)
    db_session.flush()
    db_session.add(
        models.PaymentEvent(
            payment_id=payment.id,
            event_type=f"payment.{status}",
            to_status=status,
            source="seed",
            payload={},
            occurred_at=ts,
        )
    )
    return payment


def _seed_overlap(db_session, merchant) -> None:
    """Both environments, same window/baseline: 4 research outcomes (2 failed)
    vs a 12-outcome real_test flood (all failed), per window."""
    for ts in (TS_BASELINE, TS_WINDOW):
        for _ in range(2):
            _add_outcome(db_session, merchant, ts=ts, source_type="simulator", success=True)
            _add_outcome(db_session, merchant, ts=ts, source_type="simulator", success=False)
        for _ in range(12):
            _add_outcome(db_session, merchant, ts=ts, source_type="razorpay_test", success=False)
    db_session.commit()


def _incident(make_incident, environment: str) -> models.Incident:
    return make_incident(
        environment=environment,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        detected_at=WINDOW_END,
    )


def test_research_incident_window_excludes_real_test_flood(
    db_session, insights_merchant, make_incident
):
    _seed_overlap(db_session, insights_merchant)
    incident = _incident(make_incident, "research")

    insights = InsightsService(db_session).incident_insights(incident.id)

    cf = insights.computed_from
    # Only the 4 simulator outcomes per window count (mixing would give 16).
    assert (cf.window_payments, cf.window_failures) == (4, 2)
    assert (cf.baseline_payments, cf.baseline_failures) == (4, 2)


def test_real_test_incident_window_excludes_research_outcomes(
    db_session, insights_merchant, make_incident
):
    _seed_overlap(db_session, insights_merchant)
    incident = _incident(make_incident, "real_test")

    insights = InsightsService(db_session).incident_insights(incident.id)

    cf = insights.computed_from
    assert (cf.window_payments, cf.window_failures) == (12, 12)
    assert (cf.baseline_payments, cf.baseline_failures) == (12, 12)


def test_fleet_benchmark_uses_only_the_incidents_environment(
    db_session, insights_merchant, make_incident
):
    """The platform callout's support counts only same-environment outcomes:
    with the flood confined to real_test, a research incident's fleet support
    can never exceed the research population."""
    _seed_overlap(db_session, insights_merchant)
    incident = _incident(make_incident, "research")

    insights = InsightsService(db_session).incident_insights(incident.id)

    if insights.platform_callout is not None:
        assert insights.platform_callout.platform_support <= 2

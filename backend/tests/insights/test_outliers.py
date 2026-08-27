"""Service-level insights tests: ranking, floors, callout, determinism.

All fixture numbers are exact (no RNG), so every expected rate/lift is
hand-computed in the assertions. Anything measured here is a synthetic-fixture
result, not a production metric.
"""

import pytest

from app.services.insights.facets import FacetOutcome
from app.services.insights.service import (
    InsightsError,
    InsightsService,
    rank_outliers,
)
from tests.insights.conftest import (
    BAD_REQUEST,
    BASELINE_START,
    EPOCH,
    TS_BASELINE,
    TS_WINDOW,
    WINDOW_END,
    WINDOW_START,
    seed_mix,
)

GATEWAY_ERROR = "GATEWAY_ERROR"


def _service(db_session) -> InsightsService:
    return InsightsService(db_session)


def _make_incident(make_incident, *, segment=None, **kw):
    return make_incident(
        window_start=kw.pop("window_start", WINDOW_START),
        window_end=kw.pop("window_end", WINDOW_END),
        detected_at=kw.pop("detected_at", WINDOW_END),
        meta={"segment": segment or {}},
        **kw,
    )


def _seed_happy_path(add, merchant):
    """Baseline: uniform 10% failure. Window: UPI collapses via icici bank with
    a brand-new error signature. Hand-computable expectations below."""
    seed_mix(
        add, merchant, ts=TS_BASELINE, method="upi", bank="hdfc", n_success=45,
        failures=[(BAD_REQUEST, "insufficient_fund")] * 4
        + [(BAD_REQUEST, "payment_timed_out")],
    )
    seed_mix(
        add, merchant, ts=TS_BASELINE, method="card", bank="hdfc", n_success=45,
        failures=[(BAD_REQUEST, "card_declined")] * 5,
    )
    seed_mix(
        add, merchant, ts=TS_WINDOW, method="upi", bank="hdfc", n_success=25,
        failures=[(BAD_REQUEST, "insufficient_fund")] * 5,
    )
    seed_mix(
        add, merchant, ts=TS_WINDOW, method="upi", bank="icici", n_success=0,
        failures=[(GATEWAY_ERROR, "bank_technical_error")] * 20,
    )
    seed_mix(
        add, merchant, ts=TS_WINDOW, method="card", bank="hdfc", n_success=45,
        failures=[(BAD_REQUEST, "card_declined")] * 5,
    )


def test_happy_path_ranks_overrepresented_facets(
    db_session, insights_merchant, add_outcome, make_incident
):
    _seed_happy_path(add_outcome, insights_merchant)
    db_session.commit()
    inc = _make_incident(make_incident)

    result = _service(db_session).incident_insights(inc.id)

    got = [(o.dimension, o.value) for o in result.outliers]
    assert got == [
        ("bank", "icici"),  # new at baseline -> ranks first
        ("error_code", "gateway_error"),  # new at baseline; dimension tie-break
        ("error_reason", "bank_technical_error"),
        ("method", "upi"),  # lift 5.0
        ("gateway", "razorpay"),  # lift 3.0
    ]

    by_facet = {(o.dimension, o.value): o for o in result.outliers}

    upi = by_facet[("method", "upi")]
    assert upi.basis == "failure_rate"
    assert upi.incident_rate == 0.5  # 25/50
    assert upi.baseline_rate == 0.1  # 5/50
    assert upi.lift == 5.0
    assert upi.support == 25
    assert upi.window_group_size == 50
    assert upi.baseline_group_size == 50
    assert upi.low_confidence is False

    icici = by_facet[("bank", "icici")]
    assert icici.incident_rate == 1.0  # 20/20
    assert icici.baseline_rate == 0.0  # absent at baseline
    assert icici.lift is None
    assert icici.support == 20

    share = by_facet[("error_reason", "bank_technical_error")]
    assert share.basis == "failure_share"
    assert share.incident_rate == round(20 / 30, 6)
    assert share.baseline_rate == 0.0
    assert share.lift is None
    assert share.window_group_size == 30  # all window failures
    assert share.baseline_group_size == 10

    # hdfc only lifted 1.25x (0.125 vs 0.10) -> below the min-lift floor
    assert ("bank", "hdfc") not in by_facet
    # card flat at 0.10 -> not an outlier
    assert ("method", "card") not in by_facet

    # No segment -> the incident slice IS the fleet, so a fleet-wide elevation
    # is the honest classification.
    callout = result.platform_callout
    assert callout is not None
    assert (callout.dimension, callout.value) == ("bank", "icici")
    assert callout.classification == "platform_wide"
    assert callout.platform_scope == "simulated_fleet"
    assert callout.platform_support == 20
    assert callout.platform_lift is None
    assert "rail-side" in callout.summary

    cf = result.computed_from
    assert cf.window_start == WINDOW_START and cf.window_end == WINDOW_END
    assert cf.baseline_start == BASELINE_START and cf.baseline_end == WINDOW_START
    assert (cf.window_payments, cf.window_failures) == (100, 30)
    assert (cf.baseline_payments, cf.baseline_failures) == (100, 10)


def test_zero_failures_returns_empty_insights(
    db_session, insights_merchant, add_outcome, make_incident
):
    seed_mix(add_outcome, insights_merchant, ts=TS_BASELINE, method="upi", bank="hdfc",
             n_success=10, failures=[])
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="upi", bank="hdfc",
             n_success=10, failures=[])
    db_session.commit()
    inc = _make_incident(make_incident)

    result = _service(db_session).incident_insights(inc.id)

    assert result.outliers == []
    assert result.platform_callout is None
    assert result.computed_from.window_failures == 0
    assert result.computed_from.baseline_failures == 0
    assert result.computed_from.window_payments == 10


def test_support_floor_suppresses_and_marks_low_confidence(
    db_session, insights_merchant, add_outcome, make_incident
):
    seed_mix(add_outcome, insights_merchant, ts=TS_BASELINE, method="card", bank="hdfc",
             n_success=90, failures=[(BAD_REQUEST, "insufficient_fund")] * 10)
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="card", bank="hdfc",
             n_success=90, failures=[(BAD_REQUEST, "insufficient_fund")] * 10)
    # 2 wallet failures: maximal lift but below the hard support floor (3).
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="wallet", bank="hdfc",
             n_success=0, failures=[(BAD_REQUEST, "bank_technical_error")] * 2)
    # 3 emi failures: listed, but below the confident-support floor (10).
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="emi", bank="hdfc",
             n_success=0, failures=[(BAD_REQUEST, "payment_timed_out")] * 3)
    db_session.commit()
    inc = _make_incident(make_incident)

    result = _service(db_session).incident_insights(inc.id)

    got = [(o.dimension, o.value) for o in result.outliers]
    # wallet (support 2) and bank_technical_error (support 2) are suppressed
    assert ("method", "wallet") not in got
    assert ("error_reason", "bank_technical_error") not in got
    assert got == [("error_reason", "payment_timed_out"), ("method", "emi")]
    assert all(o.support == 3 for o in result.outliers)
    assert all(o.low_confidence is True for o in result.outliers)


def test_absolute_delta_floor_suppresses_tiny_bases(
    db_session, insights_merchant, add_outcome, make_incident
):
    # 1% -> 3% failure rate: lift 3.0 but only +2pp — noise, not an outlier.
    seed_mix(add_outcome, insights_merchant, ts=TS_BASELINE, method="upi", bank="hdfc",
             n_success=198, failures=[(BAD_REQUEST, "insufficient_fund")] * 2)
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="upi", bank="hdfc",
             n_success=194, failures=[(BAD_REQUEST, "insufficient_fund")] * 6)
    db_session.commit()
    inc = _make_incident(make_incident)

    result = _service(db_session).incident_insights(inc.id)

    assert result.outliers == []
    assert result.platform_callout is None
    assert result.computed_from.window_failures == 6


def test_min_lift_floor(
    db_session, insights_merchant, add_outcome, make_incident
):
    # 20% -> 28%: +8pp clears the delta floor but lift 1.4 < 1.5.
    seed_mix(add_outcome, insights_merchant, ts=TS_BASELINE, method="card", bank="hdfc",
             n_success=160, failures=[(BAD_REQUEST, "card_declined")] * 40)
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="card", bank="hdfc",
             n_success=144, failures=[(BAD_REQUEST, "card_declined")] * 56)
    db_session.commit()
    inc = _make_incident(make_incident)

    result = _service(db_session).incident_insights(inc.id)
    assert result.outliers == []


def test_zero_baseline_failures_all_lifts_null(
    db_session, insights_merchant, add_outcome, make_incident
):
    seed_mix(add_outcome, insights_merchant, ts=TS_BASELINE, method="upi", bank="hdfc",
             n_success=20, failures=[])
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="upi", bank="hdfc",
             n_success=15, failures=[(BAD_REQUEST, "bank_technical_error")] * 5)
    db_session.commit()
    inc = _make_incident(make_incident)

    result = _service(db_session).incident_insights(inc.id)

    assert len(result.outliers) == 5
    assert all(o.lift is None for o in result.outliers)
    assert all(o.low_confidence is True for o in result.outliers)  # support 5
    assert [o.dimension for o in result.outliers] == [
        "bank", "error_code", "error_reason", "gateway", "method",
    ]
    callout = result.platform_callout
    assert callout is not None
    assert callout.platform_lift is None
    assert callout.platform_baseline_rate == 0.0
    assert "absent at baseline" in callout.summary


def test_deterministic_repeat_computation(
    db_session, insights_merchant, add_outcome, make_incident
):
    _seed_happy_path(add_outcome, insights_merchant)
    db_session.commit()
    inc = _make_incident(make_incident)

    first = _service(db_session).incident_insights(inc.id)
    second = _service(db_session).incident_insights(inc.id)
    assert first == second  # frozen dataclasses: full structural equality


def test_ranking_tie_breaks_are_deterministic():
    """Equal lifts -> support desc, then (dimension, value) asc. Pure, DB-free."""

    def outcome(method: str, success: bool) -> FacetOutcome:
        return FacetOutcome(
            payment_id="p",
            success=success,
            facets={
                "method": method,
                "bank": "hdfc",
                "gateway": "razorpay",
                "error_code": BAD_REQUEST.lower(),
                "error_reason": "insufficient_fund",
            },
        )

    window = (
        [outcome("card", True)] * 5 + [outcome("card", False)] * 5
        + [outcome("upi", True)] * 5 + [outcome("upi", False)] * 5
    )
    baseline = (
        [outcome("card", True)] * 9 + [outcome("card", False)]
        + [outcome("upi", True)] * 9 + [outcome("upi", False)]
    )

    outliers = rank_outliers(window, baseline)

    # bank/gateway: 10/20 vs 2/20 (lift 5, support 10);
    # methods: 5/10 vs 1/10 (lift 5, support 5). error facets flat -> absent.
    assert [(o.dimension, o.value) for o in outliers] == [
        ("bank", "hdfc"),
        ("gateway", "razorpay"),
        ("method", "card"),
        ("method", "upi"),
    ]
    assert all(o.lift == 5.0 for o in outliers)


def test_segmented_incident_callout_incident_specific(
    db_session, insights_merchant, add_outcome, make_incident
):
    """UPI-only incident: payment_timed_out spikes inside the UPI slice, but
    the huge card failure volume keeps the fleet-wide share near baseline."""
    seed_mix(add_outcome, insights_merchant, ts=TS_BASELINE, method="upi", bank="hdfc",
             n_success=45, failures=[(BAD_REQUEST, "insufficient_fund")] * 5)
    seed_mix(add_outcome, insights_merchant, ts=TS_BASELINE, method="card", bank="hdfc",
             n_success=700,
             failures=[(BAD_REQUEST, "card_declined")] * 270
             + [(BAD_REQUEST, "payment_timed_out")] * 30)
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="upi", bank="hdfc",
             n_success=25,
             failures=[(BAD_REQUEST, "insufficient_fund")] * 5
             + [(BAD_REQUEST, "payment_timed_out")] * 20)
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="card", bank="hdfc",
             n_success=700,
             failures=[(BAD_REQUEST, "card_declined")] * 270
             + [(BAD_REQUEST, "payment_timed_out")] * 30)
    db_session.commit()
    inc = _make_incident(make_incident, segment={"method": "upi"})

    result = _service(db_session).incident_insights(inc.id)

    # Slice-only support: the 1000-payment card stream is outside the segment.
    cf = result.computed_from
    assert cf.segment == {"method": "upi"}
    assert (cf.window_payments, cf.window_failures) == (50, 25)
    assert (cf.baseline_payments, cf.baseline_failures) == (50, 5)

    top = result.outliers[0]
    assert (top.dimension, top.value) == ("error_reason", "payment_timed_out")
    assert top.incident_rate == 0.8  # 20/25 within the UPI slice
    assert top.baseline_rate == 0.0
    assert top.lift is None

    callout = result.platform_callout
    assert callout is not None
    assert callout.classification == "incident_specific"
    assert callout.platform_scope == "simulated_fleet"
    # Fleet-wide the reason barely moved: 30/305 -> 50/325 (+5.5pp < 10pp floor)
    assert callout.platform_baseline_rate == round(30 / 305, 6)
    assert callout.platform_window_rate == round(50 / 325, 6)
    assert callout.platform_support == 50
    assert "merchant-specific" in callout.summary


def test_segmented_incident_callout_platform_wide(
    db_session, insights_merchant, add_outcome, make_incident
):
    """icici bank collapses across BOTH methods -> rail-side, fleet-wide."""
    seed_mix(add_outcome, insights_merchant, ts=TS_BASELINE, method="upi", bank="hdfc",
             n_success=23, failures=[(BAD_REQUEST, "insufficient_fund")] * 2)
    seed_mix(add_outcome, insights_merchant, ts=TS_BASELINE, method="upi", bank="icici",
             n_success=24, failures=[(BAD_REQUEST, "insufficient_fund")])
    seed_mix(add_outcome, insights_merchant, ts=TS_BASELINE, method="card", bank="hdfc",
             n_success=23, failures=[(BAD_REQUEST, "card_declined")] * 2)
    seed_mix(add_outcome, insights_merchant, ts=TS_BASELINE, method="card", bank="icici",
             n_success=24, failures=[(BAD_REQUEST, "card_declined")])
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="upi", bank="hdfc",
             n_success=23, failures=[(BAD_REQUEST, "insufficient_fund")] * 2)
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="upi", bank="icici",
             n_success=5, failures=[(GATEWAY_ERROR, "bank_technical_error")] * 20)
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="card", bank="hdfc",
             n_success=23, failures=[(BAD_REQUEST, "card_declined")] * 2)
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="card", bank="icici",
             n_success=7, failures=[(GATEWAY_ERROR, "bank_technical_error")] * 18)
    db_session.commit()
    inc = _make_incident(make_incident, segment={"method": "upi"})

    result = _service(db_session).incident_insights(inc.id)

    by_facet = {(o.dimension, o.value): o for o in result.outliers}
    icici = by_facet[("bank", "icici")]
    assert icici.incident_rate == 0.8  # 20/25 inside the UPI slice
    assert icici.baseline_rate == 0.04  # 1/25
    assert icici.lift == 20.0

    callout = result.platform_callout
    assert callout is not None
    assert callout.classification == "platform_wide"
    assert "rail-side" in callout.summary
    assert callout.platform_scope == "simulated_fleet"


def test_invalid_window_raises_insights_error(db_session, make_incident):
    inc = _make_incident(
        make_incident, window_start=EPOCH, window_end=EPOCH, detected_at=EPOCH
    )
    with pytest.raises(InsightsError):
        _service(db_session).incident_insights(inc.id)


def test_unknown_incident_raises_insights_error(db_session):
    with pytest.raises(InsightsError):
        _service(db_session).incident_insights("inc_missing")

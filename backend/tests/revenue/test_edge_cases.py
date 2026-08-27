"""Zero-signal and degenerate-window edge cases.

The contract being pinned down: when evidence is thin the engine must say so
(wide band + low confidence), and must NEVER emit a falsely precise point.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.revenue import RevenueService

REF = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
DEFAULT_WINDOW = timedelta(hours=1)
DEFAULT_BASELINE = timedelta(days=7)


def _incident(make_incident, **kw):
    kw.setdefault("window_start", REF)
    kw.setdefault("window_end", REF + DEFAULT_WINDOW)
    return make_incident(**kw)


def test_no_baseline_data_gives_wide_band_and_no_point(
    db_session, make_incident, plant_payments
):
    # Nothing before the incident window; 100 failed payments inside it.
    plant_payments(n=100, captured=0, start=REF + timedelta(seconds=5))

    report = RevenueService(db_session).revenue_at_risk(_incident(make_incident).id)
    loss = report.observed_loss

    assert loss.point_paise is None  # no defensible point
    assert loss.lower_paise == 0
    assert loss.upper_paise == 10_000_000  # full attempted volume: 100 x 100_000
    assert loss.confidence == 0.0
    assert loss.low_confidence is True
    assert "zero baseline signal" in loss.basis

    seg = report.segments[0]
    assert seg.baseline_n == 0
    assert seg.baseline_success_rate is None
    assert seg.baseline_rate_ci == (0.0, 1.0)

    # Recoverable inherits the honesty: no point, band present.
    assert report.recoverable.point_paise is None
    assert report.recoverable.upper_paise > 0
    # Strategy numbers likewise carry no fake point.
    for est in report.expected_recovery_by_strategy.values():
        assert est.point_paise is None


def test_no_payments_at_all_is_zero_not_crash(db_session, make_incident):
    report = RevenueService(db_session).revenue_at_risk(_incident(make_incident).id)
    assert report.observed_loss.point_paise == 0
    assert report.observed_loss.upper_paise == 0
    assert report.observed_loss.low_confidence is True
    assert report.recoverable.point_paise == 0
    assert report.segments == []
    assert report.failure_classes == []
    assert report.actual_recovered_paise == 0


def test_tiny_baseline_sample_is_low_confidence_but_computed(
    db_session, make_incident, plant_payments
):
    # 3 baseline payments (all captured) — a real but tiny signal.
    plant_payments(n=3, captured=3, start=REF - timedelta(days=1))
    plant_payments(n=100, captured=50, start=REF + timedelta(seconds=5))

    report = RevenueService(db_session).revenue_at_risk(_incident(make_incident).id)
    loss = report.observed_loss

    assert loss.point_paise is not None  # a point exists...
    assert loss.low_confidence is True  # ...but it is explicitly not trusted
    assert loss.confidence < 0.5
    # Wilson with n=3 is wide (rate CI ~[0.44, 1.0]): the band must cover a
    # large share of the 10_000_000 attempted volume.
    width = loss.upper_paise - loss.lower_paise
    assert width >= 4_500_000


def test_pending_payments_do_not_inflate_loss(db_session, make_incident, plant_payments):
    plant_payments(n=400, captured=360, start=REF - DEFAULT_BASELINE + timedelta(minutes=1))
    # In-window payments all still in-flight (authorized, not captured/failed).
    import app.models as models

    merchant = db_session.query(models.Merchant).first()
    for i in range(20):
        db_session.add(
            models.Payment(
                merchant_id=merchant.id,
                amount_paise=100_000,
                method="upi",
                status="authorized",
                captured=False,
                created_at=REF + timedelta(seconds=i),
            )
        )
    db_session.commit()

    report = RevenueService(db_session).revenue_at_risk(_incident(make_incident).id)
    assert report.segments == []
    assert report.observed_loss.point_paise == 0


def test_recoverable_never_exceeds_observed_loss(db_session, make_incident, plant_payments):
    plant_payments(n=400, captured=360, start=REF - DEFAULT_BASELINE + timedelta(minutes=1))
    plant_payments(
        n=100,
        captured=50,
        start=REF + timedelta(seconds=5),
        failure_reasons=[
            "payment_timed_out",
            "insufficient_fund",
            "card_number_invalid",
            "payment_cancelled",
        ],
    )
    report = RevenueService(db_session).revenue_at_risk(_incident(make_incident).id)

    assert report.recoverable.point_paise <= report.observed_loss.point_paise
    assert report.recoverable.upper_paise <= report.observed_loss.upper_paise
    for est in report.expected_recovery_by_strategy.values():
        assert est.point_paise <= report.recoverable.point_paise
        assert est.upper_paise <= report.recoverable.upper_paise


def test_partial_signal_marks_report_low_confidence(db_session, make_incident, plant_payments):
    # upi has a solid baseline; card appears only in the incident window.
    base = REF - DEFAULT_BASELINE + timedelta(minutes=1)
    plant_payments(n=400, captured=360, start=base)
    plant_payments(n=100, captured=50, start=REF + timedelta(seconds=5))
    plant_payments(n=10, captured=0, start=REF + timedelta(seconds=5), method="card")

    report = RevenueService(db_session).revenue_at_risk(_incident(make_incident).id)

    assert report.observed_loss.point_paise is not None  # known segments still sum
    assert report.observed_loss.low_confidence is True  # but the gap is flagged
    assert "zero baseline signal" in report.observed_loss.basis

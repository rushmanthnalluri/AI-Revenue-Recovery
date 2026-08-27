"""Planted-population tests: synthetic segments with exact known rates, so the
engine's estimates can be checked against hand-computed counterfactuals.

All numbers here are synthetic/preliminary by construction (tiny fixed
populations), used only to verify the methodology's arithmetic.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.ports import ActionType, RecoveryStatus
from app.services.revenue import RevenueService

REF = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
DEFAULT_WINDOW = timedelta(hours=1)
DEFAULT_BASELINE = timedelta(days=7)


def _incident(make_incident, **kw):
    kw.setdefault("window_start", REF)
    kw.setdefault("window_end", REF + DEFAULT_WINDOW)
    return make_incident(**kw)


def _baseline_start():
    return REF - DEFAULT_BASELINE


def test_single_segment_planted_rates(db_session, make_incident, plant_payments):
    # Baseline: 400 upi payments @ Rs.1000, exactly 90% captured.
    plant_payments(n=400, captured=360, start=_baseline_start() + timedelta(minutes=1))
    # Incident window: 100 payments, degraded to exactly 50% captured.
    plant_payments(n=100, captured=50, start=REF + timedelta(seconds=10))

    incident = _incident(make_incident)
    report = RevenueService(db_session).revenue_at_risk(incident.id)

    # Hand-computed counterfactual: 100 x 0.90 x 100_000 = 9_000_000 expected,
    # 5_000_000 captured -> true loss 4_000_000 paise.
    loss = report.observed_loss
    assert loss.point_paise is not None
    assert abs(loss.point_paise - 4_000_000) <= 0.015 * 4_000_000
    assert loss.lower_paise < 4_000_000 < loss.upper_paise
    # n=400 baseline -> full confidence, not flagged.
    assert loss.confidence == pytest.approx(1.0)
    assert loss.low_confidence is False

    # Every failure was payment_timed_out -> recoverability 0.70.
    recoverable = report.recoverable
    assert recoverable.point_paise is not None
    assert abs(recoverable.point_paise - 2_800_000) <= 0.02 * 2_800_000
    assert recoverable.point_paise <= loss.point_paise
    assert recoverable.upper_paise <= loss.upper_paise

    # Retry prior 0.50 on top of recoverable.
    retry = report.expected_recovery_by_strategy[ActionType.RETRY_PAYMENT.value]
    assert retry.point_paise is not None
    assert abs(retry.point_paise - 1_400_000) <= 0.03 * 1_400_000
    assert retry.point_paise <= recoverable.point_paise

    # Nothing executed yet: measured recovery is exactly zero.
    assert report.actual_recovered_paise == 0
    assert report.recovered_actions_count == 0

    # One segment, one failure class.
    assert len(report.segments) == 1
    seg = report.segments[0]
    assert seg.baseline_n == 400
    assert seg.baseline_success_rate == pytest.approx(0.90)
    assert seg.counterfactual_expected_paise == 9_000_000
    assert [fc.failure_class for fc in report.failure_classes] == ["timeout"]
    assert report.failure_classes[0].failed_count == 50


def test_multi_segment_breakdown_sums_to_total(
    db_session, make_incident, make_customer, plant_payments
):
    returning = make_customer()
    newcomer = make_customer()  # no captured history -> "new"
    # Anchor: a captured payment long before the baseline window.
    plant_payments(
        n=1, captured=1, start=_baseline_start() - timedelta(days=1), customer_id=returning.id
    )
    base = _baseline_start() + timedelta(minutes=1)
    # Two segments with different baselines and ticket sizes:
    #   upi / <=50000 / returning: 80% of 200 @ Rs.200
    plant_payments(
        n=200, captured=160, start=base, amount_paise=20_000, customer_id=returning.id
    )
    #   card / 50000_200000 / new: 95% of 100 @ Rs.1500
    plant_payments(
        n=100,
        captured=95,
        start=base,
        amount_paise=150_000,
        method="card",
        step_seconds=2,
        customer_id=newcomer.id,
    )
    win = REF + timedelta(seconds=5)
    # Incident window, both segments degraded to 50%.
    plant_payments(
        n=40, captured=20, start=win, amount_paise=20_000, customer_id=returning.id
    )
    plant_payments(
        n=20,
        captured=10,
        start=win,
        amount_paise=150_000,
        method="card",
        step_seconds=3,
        customer_id=newcomer.id,
    )

    report = RevenueService(db_session).revenue_at_risk(_incident(make_incident).id)

    assert len(report.segments) == 2
    seg_total = sum(s.observed_loss.point_paise for s in report.segments)
    assert report.observed_loss.point_paise == seg_total

    # Hand-computed per-segment losses:
    #   upi/returning: 40 x 0.80 x 20_000 - 20 x 20_000 = 240_000
    #   card/new:      20 x 0.95 x 150_000 - 10 x 150_000 = 1_350_000
    by_key = {s.segment_key: s for s in report.segments}
    upi_seg = by_key["method=upi|band=le_50000|customer=returning"]
    card_seg = by_key["method=card|band=50000_200000|customer=new"]
    assert abs(upi_seg.observed_loss.point_paise - 240_000) <= 0.02 * 240_000
    assert abs(card_seg.observed_loss.point_paise - 1_350_000) <= 0.02 * 1_350_000


def test_failure_class_mix_drives_recoverable(db_session, make_incident, plant_payments):
    plant_payments(n=400, captured=360, start=_baseline_start() + timedelta(minutes=1))
    # Half the 50 failures are timeout (0.70), half insufficient_fund (0.20).
    plant_payments(
        n=100,
        captured=50,
        start=REF + timedelta(seconds=10),
        failure_reasons=["payment_timed_out", "insufficient_fund"],
    )

    report = RevenueService(db_session).revenue_at_risk(_incident(make_incident).id)

    classes = {fc.failure_class: fc for fc in report.failure_classes}
    assert set(classes) == {"timeout", "insufficient_funds"}
    assert classes["timeout"].failed_count == 25
    assert classes["insufficient_funds"].failed_count == 25

    # Equal amounts -> equal loss share -> recoverable = loss x (0.5*0.7 + 0.5*0.2)
    loss_point = report.observed_loss.point_paise
    expected_recoverable = loss_point * 0.45
    assert abs(report.recoverable.point_paise - expected_recoverable) <= 0.02 * loss_point
    # Per-class: timeout row recovers 0.70 of its half, insufficient 0.20.
    t = classes["timeout"]
    assert abs(t.recoverable.point_paise - t.allocated_loss.point_paise * 0.70) <= 2
    i = classes["insufficient_funds"]
    assert abs(i.recoverable.point_paise - i.allocated_loss.point_paise * 0.20) <= 2


def test_incident_without_window_falls_back_to_detected_at(
    db_session, make_incident, plant_payments
):
    plant_payments(n=400, captured=360, start=_baseline_start() + timedelta(minutes=1))
    plant_payments(n=100, captured=50, start=REF - timedelta(minutes=30))
    incident = make_incident(detected_at=REF)  # no window_start/window_end

    report = RevenueService(db_session).revenue_at_risk(incident.id)

    assert report.window_end == REF
    assert report.window_start == REF - timedelta(hours=1)
    assert abs(report.observed_loss.point_paise - 4_000_000) <= 0.015 * 4_000_000


def test_unknown_incident_raises(db_session):
    with pytest.raises(ValueError, match="incident not found"):
        RevenueService(db_session).revenue_at_risk("inc_missing")


def test_actual_recovered_reads_verified_actions(
    db_session, make_incident, make_action, make_opportunity, plant_payments
):
    plant_payments(n=400, captured=360, start=_baseline_start() + timedelta(minutes=1))
    plant_payments(n=100, captured=50, start=REF + timedelta(seconds=10))
    incident = _incident(make_incident)

    opp = make_opportunity(incident_id=incident.id)
    make_action(
        opportunity=opp,
        incident_id=incident.id,
        status=RecoveryStatus.RECOVERED,
        amount_paise=300_000,
        verified_at=REF + timedelta(hours=2),
    )
    make_action(
        opportunity=opp,
        incident_id=incident.id,
        status=RecoveryStatus.RECOVERED,
        amount_paise=150_000,
        verified_at=REF + timedelta(hours=3),
    )
    # Must NOT count: failed and unknown outcomes.
    make_action(opportunity=opp, incident_id=incident.id, status=RecoveryStatus.FAILED)
    make_action(opportunity=opp, incident_id=incident.id, status=RecoveryStatus.UNKNOWN)

    report = RevenueService(db_session).revenue_at_risk(incident.id)
    assert report.actual_recovered_paise == 450_000
    assert report.recovered_actions_count == 2

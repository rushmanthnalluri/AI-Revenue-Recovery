"""Monotonicity contracts on the tunable priors and on engine outputs.

The ordering of recoverability factors is part of the methodology's
credibility story: transient failures must be more recoverable than
customer-intent ones, which must be more recoverable than funds/permanent
ones. These tests pin that ordering so a careless config edit breaks loudly.
"""

from datetime import datetime, timedelta, timezone

from app.ports import ActionType
from app.services.revenue import DEFAULT_CONFIG, RevenueService
from app.services.revenue.classify import FailureClass

REF = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
DEFAULT_WINDOW = timedelta(hours=1)
DEFAULT_BASELINE = timedelta(days=7)


def test_recoverability_factors_are_probabilities():
    for factor in DEFAULT_CONFIG.recoverability.values():
        assert 0.0 <= factor <= 1.0


def test_recoverability_ordering():
    r = DEFAULT_CONFIG.recoverability
    assert r[FailureClass.TIMEOUT] >= r[FailureClass.SOFT_DECLINE]
    assert r[FailureClass.SOFT_DECLINE] > r[FailureClass.ABANDONMENT]
    assert r[FailureClass.ABANDONMENT] > r[FailureClass.INSUFFICIENT_FUNDS]
    assert r[FailureClass.INSUFFICIENT_FUNDS] >= r[FailureClass.HARD_DECLINE]
    # Unknown is conservative: below abandonment, at or above hard decline.
    assert r[FailureClass.HARD_DECLINE] <= r[FailureClass.UNKNOWN] < r[FailureClass.ABANDONMENT]


def test_strategy_effectiveness_are_probabilities():
    for eff in DEFAULT_CONFIG.strategy_effectiveness.values():
        assert 0.0 <= eff <= 1.0


def test_strategy_ordering_and_zero_effectiveness_actions():
    e = DEFAULT_CONFIG.strategy_effectiveness
    assert e[ActionType.RETRY_PAYMENT] >= e[ActionType.CREATE_PAYMENT_LINK]
    assert e[ActionType.CREATE_PAYMENT_LINK] >= e[ActionType.NOTIFY_CUSTOMER]
    # Protective / non-recovery actions must never claim recovered revenue.
    assert e[ActionType.REFUND] == 0.0
    assert e[ActionType.NO_ACTION] == 0.0
    assert e[ActionType.PAUSE_SUBSCRIPTION] == 0.0


def test_amount_band_edges_sorted():
    edges = DEFAULT_CONFIG.amount_band_edges_paise
    assert list(edges) == sorted(edges)
    assert all(e > 0 for e in edges)


def test_engine_recoverable_monotonic_in_failure_class(db_session, make_incident, plant_payments):
    """Same degradation, three planted failure classes in equal shares ->
    per-class recoverable must follow the factor ordering."""
    plant_payments(n=400, captured=360, start=REF - DEFAULT_BASELINE + timedelta(minutes=1))
    # 90 failures, 30 of each class, equal amounts -> equal loss shares.
    plant_payments(
        n=150,
        captured=60,
        start=REF + timedelta(seconds=5),
        failure_reasons=["payment_timed_out", "insufficient_fund", "card_number_invalid"],
    )
    incident = make_incident(window_start=REF, window_end=REF + DEFAULT_WINDOW)
    report = RevenueService(db_session).revenue_at_risk(incident.id)

    classes = {fc.failure_class: fc for fc in report.failure_classes}
    assert classes["timeout"].failed_count == 30
    assert classes["insufficient_funds"].failed_count == 30
    assert classes["hard_decline"].failed_count == 30
    # Equal allocations -> recoverable ordering is purely factor-driven.
    assert (
        classes["timeout"].recoverable.point_paise
        > classes["insufficient_funds"].recoverable.point_paise
        > classes["hard_decline"].recoverable.point_paise
    )

"""opportunity_estimate: prior-based planning numbers for single opportunities."""

import pytest

from app.ports import ActionType
from app.services.revenue import RevenueService


def test_estimate_uses_payment_failure_class(db_session, make_payment, make_opportunity):
    payment = make_payment(
        amount_paise=100_000,
        status="failed",
        meta={"error_reason": "insufficient_fund"},
    )
    opp = make_opportunity(payment_id=payment.id, amount_paise=100_000)

    est = RevenueService(db_session).opportunity_estimate(opp)

    assert est.failure_class == "insufficient_funds"
    assert est.failure_class_source == "payment"
    assert est.recoverability_factor == pytest.approx(0.20)
    # recoverable = amount x factor; expected = recoverable x effectiveness
    assert est.recoverable.point_paise == 20_000
    retry = est.expected_recovery_by_strategy[ActionType.RETRY_PAYMENT.value]
    assert retry.point_paise == 10_000
    # Single-payment Bernoulli: band spans the full amount, always flagged.
    assert est.recoverable.lower_paise == 0
    assert est.recoverable.upper_paise == 100_000
    assert est.recoverable.low_confidence is True
    # Highest-effectiveness strategy is the recommendation.
    assert est.recommended_action_type == ActionType.RETRY_PAYMENT.value


def test_timeout_is_worth_more_than_insufficient_funds(
    db_session, make_payment, make_opportunity
):
    p_timeout = make_payment(meta={"error_reason": "payment_timed_out"})
    p_funds = make_payment(meta={"error_reason": "insufficient_fund"})
    svc = RevenueService(db_session)
    est_timeout = svc.opportunity_estimate(make_opportunity(payment_id=p_timeout.id))
    est_funds = svc.opportunity_estimate(make_opportunity(payment_id=p_funds.id))
    assert est_timeout.recoverable.point_paise > est_funds.recoverable.point_paise


def test_opportunity_type_default_when_no_payment(db_session, make_opportunity):
    opp = make_opportunity(opportunity_type="dropped_checkout")
    est = RevenueService(db_session).opportunity_estimate(opp)
    assert est.failure_class == "abandonment"
    assert est.failure_class_source == "opportunity_type_default"
    assert est.recoverable.point_paise == 35_000  # 100_000 x 0.35


def test_unknown_payment_class_falls_back_to_opportunity_type_default(
    db_session, make_payment, make_opportunity
):
    """stuck_checkout_payment: the attached payment's empty telemetry
    classifies as unknown, so the estimate falls back to the opportunity-type
    class default instead of pricing at the unknown floor (docs/recovery.md)."""
    payment = make_payment(amount_paise=100_000, status="created")
    opp = make_opportunity(
        opportunity_type="stuck_checkout_payment",
        payment_id=payment.id,
        amount_paise=100_000,
    )
    est = RevenueService(db_session).opportunity_estimate(opp)
    assert est.failure_class == "abandonment"
    assert est.failure_class_source == "opportunity_type_default"
    assert est.recoverability_factor == pytest.approx(0.35)
    assert est.recoverable.point_paise == 35_000  # was 10_000 at the unknown floor


def test_unknown_payment_class_with_unknown_type_stays_at_floor(
    db_session, make_payment, make_opportunity
):
    """failed_payment_retry's default is UNKNOWN too — behavior unchanged."""
    payment = make_payment(amount_paise=100_000, status="created")
    opp = make_opportunity(payment_id=payment.id, amount_paise=100_000)
    est = RevenueService(db_session).opportunity_estimate(opp)
    assert est.failure_class == "unknown"
    assert est.recoverable.point_paise == 10_000  # 100_000 x 0.10


def test_unknown_type_falls_back_to_unknown_class(db_session, make_opportunity):
    opp = make_opportunity(opportunity_type="something_new")
    est = RevenueService(db_session).opportunity_estimate(opp)
    assert est.failure_class == "unknown"
    assert est.recoverable.point_paise == 10_000  # 100_000 x 0.10


def test_single_action_type_filter(db_session, make_opportunity):
    opp = make_opportunity()
    est = RevenueService(db_session).opportunity_estimate(
        opp, action_type=ActionType.CREATE_PAYMENT_LINK
    )
    assert list(est.expected_recovery_by_strategy) == [ActionType.CREATE_PAYMENT_LINK.value]
    # unknown class (no payment, failed_payment_retry default) x link prior
    assert est.expected_recovery_by_strategy["create_payment_link"].point_paise == 3_000


def test_accepts_id_string(db_session, make_opportunity):
    opp = make_opportunity()
    est = RevenueService(db_session).opportunity_estimate(opp.id)
    assert est.opportunity_id == opp.id


def test_unknown_id_raises(db_session):
    with pytest.raises(ValueError, match="opportunity not found"):
        RevenueService(db_session).opportunity_estimate("opp_missing")

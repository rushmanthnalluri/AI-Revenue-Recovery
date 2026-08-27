"""Failure-class classification: known Razorpay reasons, defensive parsing."""

import pytest

from app.services.revenue.classify import FailureClass, classify_failure, classify_reason


@pytest.mark.parametrize(
    "reason",
    ["payment_timed_out", "timed_out", "gateway timeout"],
)
def test_timeout(reason):
    assert classify_reason(meta={"error_reason": reason}) is FailureClass.TIMEOUT


@pytest.mark.parametrize("reason", ["insufficient_fund", "insufficient_balance"])
def test_insufficient_funds(reason):
    assert classify_reason(meta={"error_reason": reason}) is FailureClass.INSUFFICIENT_FUNDS


@pytest.mark.parametrize(
    "reason",
    [
        "card_number_invalid",
        "card_disabled_for_online_payments",
        "authentication_failed",
        "pin_attempts_exceeded",
        "debit_instrument_blocked",
        "lost_card",
    ],
)
def test_hard_decline(reason):
    assert classify_reason(meta={"error_reason": reason}) is FailureClass.HARD_DECLINE


@pytest.mark.parametrize(
    "reason",
    ["payment_cancelled", "incorrect_otp", "incorrect_pin"],
)
def test_abandonment(reason):
    assert classify_reason(meta={"error_reason": reason}) is FailureClass.ABANDONMENT


@pytest.mark.parametrize(
    "reason",
    [
        "card_declined",
        "payment_declined",
        "gateway_technical_error",
        "bank_technical_error",
        "transaction_limit_exceeded",
        "duplicate_request",
    ],
)
def test_soft_decline(reason):
    assert classify_reason(meta={"error_reason": reason}) is FailureClass.SOFT_DECLINE


def test_unlisted_reason_is_unknown():
    assert classify_reason(meta={"error_reason": "fraud_suspected_xyz"}) is FailureClass.UNKNOWN


def test_no_signal_is_unknown():
    assert classify_reason() is FailureClass.UNKNOWN
    assert classify_reason(meta={}) is FailureClass.UNKNOWN


def test_error_source_fallback_when_no_reason():
    assert classify_reason(error_source="bank") is FailureClass.SOFT_DECLINE
    assert classify_reason(error_source="gateway") is FailureClass.SOFT_DECLINE
    assert classify_reason(error_source="customer") is FailureClass.ABANDONMENT


def test_reason_beats_source_fallback():
    # insufficient_fund is often reported with error_source=customer; the
    # reason must win over the coarser source hint.
    assert (
        classify_reason(error_source="customer", meta={"error_reason": "insufficient_fund"})
        is FailureClass.INSUFFICIENT_FUNDS
    )


def test_defensive_normalization():
    assert (
        classify_reason(error_description="Payment Timed-Out at gateway")
        is FailureClass.TIMEOUT
    )
    assert (
        classify_reason(error_code="BAD_REQUEST_ERROR", error_description=None)
        is FailureClass.UNKNOWN
    )


def test_classify_failure_reads_payment_attributes():
    class P:
        error_code = "BAD_REQUEST_ERROR"
        error_description = None
        error_source = "bank"
        meta = {"error_reason": "payment_timed_out"}

    assert classify_failure(P()) is FailureClass.TIMEOUT

    class Q:
        error_code = None
        error_description = None
        error_source = None
        meta = None

    assert classify_failure(Q()) is FailureClass.UNKNOWN

"""Feature-engineering correctness: exact values on handcrafted records,
the FEATURE_NAMES contract, vectorization, and DB window extraction."""

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.models import Payment, PaymentEvent
from app.services.diagnosis.features import (
    FEATURE_NAMES,
    compute_features,
    compute_features_for_incident,
    features_to_vector,
    load_window_records,
)

T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def rec(**kw) -> dict:
    base = {
        "outcome": "captured",
        "method": "upi",
        "bank": None,
        "error_source": None,
        "error_step": None,
        "error_reason": None,
        "latency_ms": 100.0,
        "is_subscription": False,
        "amount_paise": 50000,
    }
    base.update(kw)
    return base


def failed(reason="bank_technical_error", source="bank", step="payment_authorization", **kw) -> dict:
    return rec(outcome="failed", error_source=source, error_step=step, error_reason=reason, **kw)


@pytest.fixture()
def handcrafted():
    baseline = [
        rec(method="upi", bank="hdfc"),
        rec(method="upi", bank="hdfc"),
        rec(method="upi", bank="icici"),
        rec(method="upi", bank="icici"),
        rec(method="card", bank="hdfc"),
        rec(method="card", bank="icici"),
        rec(method="netbanking", bank="sbi"),
        rec(method="netbanking", bank="axis"),
        failed(reason="incorrect_otp", source="customer", step="payment_authentication",
               method="upi", bank="hdfc", latency_ms=120.0),
        rec(outcome="pending", method="wallet"),
    ]
    window = [
        rec(method="upi", bank="hdfc"),
        rec(method="upi", bank="hdfc"),
        rec(method="card", bank="icici"),
        rec(method="netbanking", bank="sbi"),
        rec(method="netbanking", bank="axis", is_subscription=True),
        failed(method="upi", bank="hdfc", latency_ms=150.0),
        failed(method="upi", bank="hdfc", latency_ms=150.0),
        failed(method="upi", bank="hdfc", latency_ms=150.0),
        failed(method="upi", bank="hdfc", latency_ms=150.0, is_subscription=True),
        rec(outcome="pending", method="card"),
    ]
    return window, baseline


def test_feature_contract(handcrafted):
    window, baseline = handcrafted
    feats = compute_features(window, baseline)
    assert set(feats.keys()) == set(FEATURE_NAMES)
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))
    assert all(type(v) is float for v in feats.values())  # JSON-safe, no np types


def test_exact_values(handcrafted):
    window, baseline = handcrafted
    f = compute_features(window, baseline)

    assert f["volume"] == 10.0
    assert f["volume_delta_ratio"] == pytest.approx(0.0)
    assert f["failed_volume"] == 4.0
    assert f["failure_rate_w"] == pytest.approx(0.4)
    assert f["failure_rate_b"] == pytest.approx(0.1)
    assert f["failure_rate_delta"] == pytest.approx(0.3)

    # upi failure rate: 4/6 in window vs 1/5 in baseline
    assert f["max_method_rate_delta"] == pytest.approx(4 / 6 - 1 / 5)
    assert f["top_method_fail_share"] == pytest.approx(1.0)
    assert f["top_method_upi"] == 1.0
    assert f["top_method_card"] == 0.0

    # hdfc failure rate: 4/6 in window vs 1/4 in baseline (2 upi + 1 card + 1 failed)
    assert f["max_bank_rate_delta"] == pytest.approx(4 / 6 - 1 / 4)
    assert f["top_bank_fail_share"] == pytest.approx(1.0)
    assert f["distinct_failing_banks_w"] == 1.0

    # error source: all window failures are bank-sourced; baseline's one failure was customer
    assert f["src_fail_share_w_bank"] == pytest.approx(1.0)
    assert f["src_fail_share_delta_bank"] == pytest.approx(1.0 - 0.0)
    assert f["src_fail_share_w_customer"] == pytest.approx(0.0)
    assert f["src_fail_share_delta_customer"] == pytest.approx(0.0 - 1.0)
    assert f["top_source_bank"] == 1.0
    assert f["top_source_gateway"] == 0.0
    assert f["max_source_share_delta"] == pytest.approx(1.0)

    # error step / reason
    assert f["top_step_payment_authorization"] == 1.0
    assert f["reason_share_w_bank_technical_error"] == pytest.approx(1.0)
    assert f["reason_share_delta_bank_technical_error"] == pytest.approx(1.0)
    assert f["reason_share_w_incorrect_otp"] == pytest.approx(0.0)

    # latency: window sorted [100]*6 + [150]*4 -> p50=100, p90=150
    # baseline sorted [100]*9 + [120] -> p50=100, p90=102 (linear interp)
    assert f["latency_coverage"] == pytest.approx(1.0)
    assert f["latency_p50_w"] == pytest.approx(100.0)
    assert f["latency_p90_w"] == pytest.approx(150.0)
    assert f["latency_p50_delta"] == pytest.approx(0.0)
    assert f["latency_p90_delta"] == pytest.approx(48.0)
    assert f["latency_p90_delta_ratio"] == pytest.approx(48.0 / 102.0)

    # abandonment proxy: 1/10 both windows
    assert f["abandonment_rate_w"] == pytest.approx(0.1)
    assert f["abandonment_rate_delta"] == pytest.approx(0.0)

    # subscriptions: 2 subs in window (1 failed), 0 in baseline
    assert f["sub_share_w"] == pytest.approx(0.2)
    assert f["sub_failure_share_w"] == pytest.approx(0.25)
    assert f["sub_failure_share_delta"] == pytest.approx(0.25)
    assert f["sub_failure_rate_delta"] == pytest.approx(0.5)


def test_empty_windows_are_zero_vector():
    feats = compute_features([], [])
    assert set(feats.keys()) == set(FEATURE_NAMES)
    assert all(v == 0.0 for v in feats.values())


def test_features_to_vector_order_and_defaults():
    names = FEATURE_NAMES
    feats = {name: float(i) for i, name in enumerate(names)}
    vec = features_to_vector(feats)
    assert vec == [float(i) for i in range(len(names))]
    assert features_to_vector({}, names) == [0.0] * len(names)


# --- DB extraction -----------------------------------------------------------


def _mk_payment(db_session, merchant, **kw):
    p = Payment(
        merchant_id=merchant.id,
        amount_paise=kw.pop("amount_paise", 50000),
        status=kw.pop("status", "created"),
        **kw,
    )
    db_session.add(p)
    db_session.flush()
    return p


def _mk_event(db_session, payment, *, to_status, occurred_at, payload=None):
    e = PaymentEvent(
        payment_id=payment.id,
        event_type=f"payment.{to_status}",
        to_status=to_status,
        occurred_at=occurred_at,
        payload=payload or {},
    )
    db_session.add(e)
    return e


def test_load_window_records_boundaries_and_latest_wins(db_session, make_merchant):
    merchant = make_merchant()
    start, end = T0, T0 + timedelta(hours=1)

    inside = _mk_payment(db_session, merchant, method="upi", status="failed")
    # earlier non-terminal event, later terminal event -> latest decides
    _mk_event(db_session, inside, to_status="created", occurred_at=start + timedelta(minutes=1))
    _mk_event(
        db_session,
        inside,
        to_status="failed",
        occurred_at=start + timedelta(minutes=2),
        payload={"error_source": "bank", "error_reason": "bank_technical_error", "latency_ms": 321},
    )
    before = _mk_payment(db_session, merchant, status="failed")
    _mk_event(db_session, before, to_status="failed", occurred_at=start - timedelta(seconds=1))
    at_end = _mk_payment(db_session, merchant, status="failed")
    _mk_event(db_session, at_end, to_status="failed", occurred_at=end)  # [start, end) -> excluded
    pending = _mk_payment(db_session, merchant, method="card", status="created")
    _mk_event(db_session, pending, to_status="created", occurred_at=start + timedelta(minutes=5))
    db_session.commit()

    records = load_window_records(db_session, start, end)
    by_outcome = {}
    for r in records:
        by_outcome.setdefault(r["outcome"], []).append(r)

    assert len(records) == 2  # before-window and at-end payments excluded
    assert by_outcome["failed"][0]["error_source"] == "bank"
    assert by_outcome["failed"][0]["error_reason"] == "bank_technical_error"
    assert by_outcome["failed"][0]["latency_ms"] == 321.0
    assert by_outcome["pending"][0]["method"] == "card"


def test_compute_features_for_incident_fallback_window(db_session, make_merchant, make_incident):
    merchant = make_merchant()
    # No explicit window on the incident -> [detected_at-1h, detected_at].
    detected = T0 + timedelta(hours=2)
    incident = make_incident(detected_at=detected)
    p = _mk_payment(db_session, merchant, method="upi", status="failed")
    _mk_event(
        db_session,
        p,
        to_status="failed",
        occurred_at=detected - timedelta(minutes=30),
        payload={"error_source": "gateway", "error_reason": "gateway_technical_error"},
    )
    db_session.commit()

    feats = compute_features_for_incident(db_session, incident)
    assert feats["volume"] == 1.0
    assert feats["failure_rate_w"] == pytest.approx(1.0)
    assert feats["src_fail_share_w_gateway"] == pytest.approx(1.0)


def test_incident_with_bad_window_raises(db_session, make_incident):
    from app.services.diagnosis.features import incident_windows

    incident = make_incident(detected_at=T0, window_start=T0, window_end=T0 - timedelta(hours=1))
    with pytest.raises(ValueError):
        incident_windows(incident)

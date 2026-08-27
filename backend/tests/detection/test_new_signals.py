"""New detection signals from the detection-recall track (docs/detection.md):

- per-route capture-latency scan (the route_latency blind spot), including
  the within-method corroboration guard against mix shift;
- ``checkout_abandonment_rate`` (the checkout_abandonment blind spot), with
  honest right-censoring of the window's tail;
- ``insufficient_fund_share`` (the small-volume error-share signal for the
  insufficient-funds wave), with its own per-metric floors.

Every signal is covered twice: a quiet control (must stay silent) and an
injected spike (must fire) — plus series-level unit tests for the censoring
and share semantics. All fixtures are exact (no randomness): rates are
realized as exact counts per bucket.
"""

from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.db import utcnow
from app.models import Incident
from app.services.detection.engine import (
    DEFAULT_MIN_ABSOLUTE_DEVIATION,
    METRIC_MIN_OBSERVED,
)
from app.services.detection.series import (
    KNOWN_METRICS,
    METRIC_CAPTURE_LATENCY,
    METRIC_CHECKOUT_ABANDONMENT,
    METRIC_INSUFFICIENT_FUND_SHARE,
    METRIC_SUCCESS_RATE,
    CheckoutAttempt,
    PaymentOutcome,
    build_abandonment_series,
    build_metric_series,
    build_series,
    floor_bucket,
    is_insufficient_fund,
    load_checkout_attempts,
)
from tests.detection.conftest import EPOCH, CheckoutStream, Stream

# ---------------------------------------------------------------------------
# series-level semantics (no DB)
# ---------------------------------------------------------------------------


def _attempt(i: int, *, abandoned, minutes: int = 0) -> CheckoutAttempt:
    return CheckoutAttempt(
        payment_id=f"pay_{i}_{minutes}",
        ts=EPOCH + timedelta(minutes=minutes),
        resolved_ts=None,
        amount_paise=10_000,
        segments={"method": "card"},
        abandoned=abandoned,
    )


class TestAbandonmentSeries:
    def test_censored_attempts_are_excluded(self):
        """Right-censoring: undecidable attempts leave neither numerator nor
        denominator — the bucket reflects only decidable traffic."""
        attempts = [
            _attempt(0, abandoned=False),
            _attempt(1, abandoned=False),
            _attempt(2, abandoned=True),
            _attempt(3, abandoned=None),  # censored
            _attempt(4, abandoned=None),  # censored
        ]
        series = build_abandonment_series(
            attempts,
            window_start=EPOCH,
            window_end=EPOCH + timedelta(minutes=30),
            bucket_minutes=30,
        )
        assert series[0].count == 3  # decidable only
        assert series[0].value == pytest.approx(1 / 3)

    def test_all_censored_bucket_is_empty(self):
        attempts = [_attempt(i, abandoned=None) for i in range(5)]
        series = build_abandonment_series(
            attempts,
            window_start=EPOCH,
            window_end=EPOCH + timedelta(minutes=30),
            bucket_minutes=30,
        )
        assert series[0].count == 0
        assert series[0].value is None

    def test_build_series_rejects_attempt_metric(self):
        with pytest.raises(ValueError, match="attempt-based"):
            build_series(
                [],
                metric=METRIC_CHECKOUT_ABANDONMENT,
                window_start=EPOCH,
                window_end=EPOCH + timedelta(hours=1),
                bucket_minutes=5,
            )

    def test_dispatch_picks_family(self):
        series = build_metric_series(
            [_attempt(0, abandoned=True), _attempt(1, abandoned=True)],
            metric=METRIC_CHECKOUT_ABANDONMENT,
            window_start=EPOCH,
            window_end=EPOCH + timedelta(minutes=30),
            bucket_minutes=30,
        )
        assert series[0].value == 1.0


class TestInsufficientFundShareSeries:
    def _outcome(self, i, *, success, reason=None, minutes=0) -> PaymentOutcome:
        return PaymentOutcome(
            payment_id=f"pay_{i}",
            ts=EPOCH + timedelta(minutes=minutes),
            success=success,
            amount_paise=10_000,
            latency_ms=None,
            error_reason=reason,
        )

    def test_share_of_failures(self):
        outcomes = [
            self._outcome(0, success=True),
            self._outcome(1, success=False, reason="insufficient_fund"),
            self._outcome(2, success=False, reason="insufficient_fund"),
            self._outcome(3, success=False, reason="payment_declined"),
        ]
        series = build_series(
            outcomes,
            metric=METRIC_INSUFFICIENT_FUND_SHARE,
            window_start=EPOCH,
            window_end=EPOCH + timedelta(minutes=30),
            bucket_minutes=30,
        )
        assert series[0].value == pytest.approx(2 / 3)
        assert series[0].count == 3  # denominator = failures, not outcomes

    def test_no_failures_bucket_is_empty(self):
        outcomes = [self._outcome(i, success=True) for i in range(4)]
        series = build_series(
            outcomes,
            metric=METRIC_INSUFFICIENT_FUND_SHARE,
            window_start=EPOCH,
            window_end=EPOCH + timedelta(minutes=30),
            bucket_minutes=30,
        )
        assert series[0].value is None
        assert series[0].count == 0

    def test_reason_matching_is_defensive(self):
        assert is_insufficient_fund("insufficient_fund")
        assert is_insufficient_fund("Insufficient Funds")
        assert is_insufficient_fund("INSUFFICIENT_BALANCE")
        assert not is_insufficient_fund("payment_declined")
        assert not is_insufficient_fund(None)
        assert not is_insufficient_fund("")


class TestMetricRegistry:
    def test_known_metrics_and_floors(self):
        assert KNOWN_METRICS == (
            METRIC_SUCCESS_RATE,
            METRIC_CAPTURE_LATENCY,
            METRIC_CHECKOUT_ABANDONMENT,
            METRIC_INSUFFICIENT_FUND_SHARE,
        )
        assert DEFAULT_MIN_ABSOLUTE_DEVIATION[METRIC_CHECKOUT_ABANDONMENT] == 0.20
        assert DEFAULT_MIN_ABSOLUTE_DEVIATION[METRIC_INSUFFICIENT_FUND_SHARE] == 0.25
        assert METRIC_MIN_OBSERVED[METRIC_CHECKOUT_ABANDONMENT] == 0.35
        assert METRIC_MIN_OBSERVED[METRIC_INSUFFICIENT_FUND_SHARE] == 0.90

    def test_api_accepts_new_metrics_and_rejects_unknown(self, client):
        r = client.post(
            "/api/v1/detection/run",
            json={"metrics": [METRIC_CHECKOUT_ABANDONMENT, METRIC_INSUFFICIENT_FUND_SHARE]},
        )
        assert r.status_code == 200, r.text
        r = client.post("/api/v1/detection/run", json={"metrics": ["nonsense"]})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# checkout_abandonment_rate — engine level
# ---------------------------------------------------------------------------

ABND_BODY = {
    "window_minutes": 240,
    "bucket_minutes": 5,
    "detector": "zscore",
    "metrics": [METRIC_CHECKOUT_ABANDONMENT],
    "baseline_buckets": 4,
}


class TestCheckoutAbandonment:
    def test_quiet_control_stays_silent(self, client, db_session, seed_checkout_events):
        """Every checkout resolves within a minute: nothing may fire."""
        seed_checkout_events(streams=[CheckoutStream(stuck_at=lambda i: 0)])
        body = client.post("/api/v1/detection/run", json=ABND_BODY).json()
        assert body["anomalies_detected"] == 0
        assert db_session.scalar(sa.select(sa.func.count()).select_from(Incident)) == 0

    def test_abandonment_spike_detected(self, client, db_session, seed_checkout_events):
        """40% of checkouts stuck from mid-window on (the last ~30 min of the
        window is right-censored, so the spike reads on buckets 24-41)."""
        seed_checkout_events(streams=[CheckoutStream(stuck_at=lambda i: 4 if i >= 24 else 0)])
        r = client.post("/api/v1/detection/run", json=ABND_BODY)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["anomalies_detected"] == 1
        view = body["incidents"][0]
        assert view["metric"] == METRIC_CHECKOUT_ABANDONMENT
        assert view["segment"] == {}
        assert view["deviation_pct"] >= 50.0
        assert 40 <= view["affected_payments_count"] <= 80
        assert view["revenue_at_risk_paise"] == view["affected_payments_count"] * 10000

        inc = db_session.get(Incident, view["incident_id"])
        kinds = {e.evidence_type for e in inc.evidence}
        assert kinds == {"metric_series", "segment_breakdown"}
        dims = next(e for e in inc.evidence if e.evidence_type == "segment_breakdown")
        assert set(dims.payload["dimensions"]) == {"method", "bank", "gateway", "route"}

    def test_loader_marks_tri_state(self, db_session, seed_checkout_events):
        """Resolved-in-time = False, stuck = True, within-threshold-of-edge =
        None (right-censored). Checked against an explicit knowledge edge."""
        seed_checkout_events(
            buckets=48, streams=[CheckoutStream(stuck_at=lambda i: 10 if 20 <= i < 40 else 0)]
        )
        anchor = floor_bucket(utcnow(), 5)
        window_end = anchor
        window_start = window_end - timedelta(hours=4)
        attempts = load_checkout_attempts(
            db_session, window_start, window_end, inactivity_minutes=30
        )
        assert attempts, "fixture produced no attempts"
        resolved = [a for a in attempts if a.abandoned is False]
        stuck = [a for a in attempts if a.abandoned is True]
        censored = [a for a in attempts if a.abandoned is None]
        assert resolved, "expected resolved attempts"
        assert stuck, "expected stuck attempts"
        # every censored attempt's threshold horizon is beyond the edge
        for a in censored:
            assert a.ts + timedelta(minutes=30) > window_end
        # every stuck attempt's horizon is inside the edge, unresolved
        for a in stuck:
            assert a.ts + timedelta(minutes=30) <= window_end
            assert a.resolved_ts is None


# ---------------------------------------------------------------------------
# insufficient_fund_share — engine level
# ---------------------------------------------------------------------------

IFS_BODY = {
    "window_minutes": 480,
    "bucket_minutes": 5,
    "detector": "zscore",
    "metrics": [METRIC_INSUFFICIENT_FUND_SHARE],
    # 8 valid 60-min buckets over the 480-min fixture; leave scored buckets
    "baseline_buckets": 4,
}


def _ifs_seed(seed_payment_events, wave_reason):
    """96 buckets (8 x 60-min metric buckets): 20 payments/bucket, 4 failures;
    the first half fails 1-of-4 on insufficient funds, the second half
    follows ``wave_reason``."""
    return seed_payment_events(
        buckets=96,
        streams=[
            Stream(
                per_bucket=20,
                rate_at=lambda i: 0.8,
                reason_at=lambda i, j: (
                    ("insufficient_fund" if j == 0 else "payment_declined")
                    if i < 48
                    else wave_reason(j)
                ),
            )
        ],
    )


class TestInsufficientFundShare:
    def test_quiet_control_stays_silent(self, client, db_session, seed_payment_events):
        """A constant 25% insufficient-fund mix is business as usual."""
        _ifs_seed(seed_payment_events, lambda j: "insufficient_fund" if j == 0 else "payment_declined")
        body = client.post("/api/v1/detection/run", json=IFS_BODY).json()
        assert body["anomalies_detected"] == 0
        assert db_session.scalar(sa.select(sa.func.count()).select_from(Incident)) == 0

    def test_funds_wave_detected(self, client, db_session, seed_payment_events):
        """The mix jumps to near-pure insufficient funds mid-window (the
        measured wave signature: a near-single-class failure hour)."""
        _ifs_seed(seed_payment_events, lambda j: "insufficient_fund")
        body = client.post("/api/v1/detection/run", json=IFS_BODY).json()
        assert body["anomalies_detected"] == 1
        view = body["incidents"][0]
        assert view["metric"] == METRIC_INSUFFICIENT_FUND_SHARE
        assert view["deviation_pct"] >= 100.0
        # affected = insufficient-fund failures in the anomaly region
        assert view["affected_payments_count"] >= 100
        assert view["revenue_at_risk_paise"] == view["affected_payments_count"] * 10000

    def test_elevated_but_mixed_wave_does_not_fire(
        self, client, db_session, seed_payment_events
    ):
        """Admission is deliberately conservative: a 75% insufficient-fund
        mix fires the z-score but is below min_observed 0.9 — measured
        organic clusters reach 0.71, so anything under 0.9 is not admitted."""
        _ifs_seed(seed_payment_events, lambda j: "insufficient_fund" if j < 3 else "payment_declined")
        body = client.post("/api/v1/detection/run", json=IFS_BODY).json()
        assert body["anomalies_detected"] == 0
        assert body["anomalies_filtered"] >= 1  # fired, but held by the floor

    def test_other_failure_wave_does_not_fire(self, client, db_session, seed_payment_events):
        """Direction + class specificity: a bank-downtime wave (0% insufficient
        funds) must not fire the funds signal."""
        _ifs_seed(seed_payment_events, lambda j: "bank_downtime")
        body = client.post("/api/v1/detection/run", json=IFS_BODY).json()
        assert body["anomalies_detected"] == 0

    def test_explicit_request_floors_beat_metric_defaults(
        self, client, db_session, seed_payment_events
    ):
        """The metric ships its own floors for the sparse night regime — but
        an explicit request field always wins over the metric default."""
        _ifs_seed(seed_payment_events, lambda j: "insufficient_fund")
        # metric default min_flagged_volume is 3 (admits); an explicit
        # request value overrides it and suppresses the incident
        body = client.post(
            "/api/v1/detection/run",
            json={**IFS_BODY, "min_flagged_volume": 1000},
        ).json()
        assert body["anomalies_detected"] == 0
        assert body["anomalies_filtered"] >= 1


# ---------------------------------------------------------------------------
# per-route latency scan — engine level
# ---------------------------------------------------------------------------

SCAN_BODY = {
    "window_minutes": 240,
    "bucket_minutes": 5,
    "detector": "zscore",
    "metrics": [METRIC_CAPTURE_LATENCY],
    "baseline_buckets": 12,
    "min_bucket_count": 5,
}


def _alternating(i: int) -> float:
    return 200.0 if i % 2 == 0 else 300.0


class TestRouteLatencyScan:
    # The edge route carries two methods so within-method corroboration is
    # decided by the robust >=2-methods rule, not the lone-method ratio (the
    # 15-min scan grid is offset from the fixture's 5-min grid, so the
    # anomaly region always contains some baseline-valued events).
    EDGE_STREAMS = [
        Stream(
            method="card",
            route="pg_edge",
            per_bucket=2,
            rate_at=lambda i: 1.0,
            latency_at=lambda i: _alternating(i) if i < 36 else 800.0,
        ),
        Stream(
            method="upi",
            route="pg_edge",
            per_bucket=2,
            rate_at=lambda i: 1.0,
            latency_at=lambda i: _alternating(i) if i < 36 else 800.0,
        ),
    ]

    def test_scan_finds_localized_route_latency(
        self, client, db_session, seed_payment_events
    ):
        """One route (20% of traffic) triples+ its latency: the aggregate
        stays under z=3 (noise-diluted), the slice fires — the blind spot."""
        seed_payment_events(
            streams=[
                Stream(
                    method="upi",
                    route="pg_main",
                    per_bucket=16,
                    rate_at=lambda i: 1.0,
                    latency_at=_alternating,
                ),
                *self.EDGE_STREAMS,
            ]
        )
        body = client.post("/api/v1/detection/run", json=SCAN_BODY).json()
        assert body["anomalies_detected"] == 1
        view = body["incidents"][0]
        assert view["metric"] == METRIC_CAPTURE_LATENCY
        assert view["segment"] == {"route": "pg_edge"}  # localized, not aggregate
        inc = db_session.get(Incident, view["incident_id"])
        assert (inc.meta or {}).get("segment_scan") is True

    def test_scan_quiet_control_stays_silent(self, client, db_session, seed_payment_events):
        seed_payment_events(
            streams=[
                Stream(
                    method="upi",
                    route="pg_main",
                    per_bucket=16,
                    rate_at=lambda i: 1.0,
                    latency_at=_alternating,
                ),
                Stream(
                    method="card",
                    route="pg_edge",
                    per_bucket=2,
                    rate_at=lambda i: 1.0,
                    latency_at=_alternating,
                ),
                Stream(
                    method="upi",
                    route="pg_edge",
                    per_bucket=2,
                    rate_at=lambda i: 1.0,
                    latency_at=_alternating,
                ),
            ]
        )
        body = client.post("/api/v1/detection/run", json=SCAN_BODY).json()
        assert body["anomalies_detected"] == 0
        assert db_session.scalar(sa.select(sa.func.count()).select_from(Incident)) == 0

    def test_scan_skipped_when_aggregate_fires(
        self, client, db_session, seed_payment_events
    ):
        """Fleet-wide latency incident: the merchant-wide series fires, so no
        duplicate slice incidents are created."""
        seed_payment_events(
            streams=[
                Stream(
                    method="upi",
                    route="pg_main",
                    per_bucket=16,
                    rate_at=lambda i: 1.0,
                    latency_at=lambda i: _alternating(i) if i < 36 else 1500.0,
                ),
                Stream(
                    method="card",
                    route="pg_edge",
                    per_bucket=4,
                    rate_at=lambda i: 1.0,
                    latency_at=lambda i: _alternating(i) if i < 36 else 1500.0,
                ),
            ]
        )
        body = client.post("/api/v1/detection/run", json=SCAN_BODY).json()
        assert body["anomalies_detected"] == 1  # aggregate only
        view = body["incidents"][0]
        assert view["metric"] == METRIC_CAPTURE_LATENCY
        assert view["segment"] == {}

    def test_mix_shift_is_not_a_route_incident(
        self, client, db_session, seed_payment_events
    ):
        """Corroboration guard: the slice mean rises because the slow method's
        SHARE rises while every method's own latency stays flat — that is mix
        shift, not route degradation, and must not become an incident."""
        seed_payment_events(
            streams=[
                # bulk traffic with noisy aggregate latency (keeps the
                # merchant-wide z-score silent throughout)
                Stream(
                    method="wallet",
                    route="pg_other",
                    per_bucket=40,
                    rate_at=lambda i: 1.0,
                    latency_at=_alternating,
                ),
                Stream(
                    method="card",
                    route="pg_main",
                    per_bucket=12,
                    rate_at=lambda i: 1.0,
                    latency_at=lambda i: 250.0,
                ),
                # slow method on the same route: its success count (and thus
                # share of the slice's latency samples) rises mid-window
                Stream(
                    method="netbanking",
                    route="pg_main",
                    per_bucket=16,
                    rate_at=lambda i: 0.25 if i < 36 else 0.75,
                    latency_at=lambda i: 1000.0,
                ),
            ]
        )
        body = client.post("/api/v1/detection/run", json=SCAN_BODY).json()
        assert body["anomalies_detected"] == 0
        assert db_session.scalar(sa.select(sa.func.count()).select_from(Incident)) == 0

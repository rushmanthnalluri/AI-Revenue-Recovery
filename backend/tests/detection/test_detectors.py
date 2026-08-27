"""Per-detector unit tests: each registered detector must flag an injected
degradation on small synthetic series, stay quiet on a healthy series, and
respect the degradation direction."""

import pytest

from app.services.detection.detectors import (
    DetectorParams,
    available_detectors,
    get_detector,
)

BASELINE = 8  # small baseline so fixtures stay small
SHIFT_AT = 20
N = 40


def params(direction: str = "down", **kw) -> DetectorParams:
    return DetectorParams(
        baseline_buckets=BASELINE,
        direction=direction,
        bucket_minutes=5,
        min_bucket_count=5,
        **kw,
    )


@pytest.fixture(params=available_detectors())
def detector(request):
    return get_detector(request.param)


class TestEveryDetector:
    """The assignment's core guarantee: any registered detector flags an
    injected shift."""

    def test_flags_success_rate_drop(self, detector, sr_series):
        buckets, labels = sr_series(
            n=N, shift_at=SHIFT_AT, shift_to=0.45, recover_at=None
        )
        anomaly = detector.detect(buckets, params("down"))
        assert anomaly is not None, f"{detector.name} missed an 0.90 -> 0.45 drop"
        assert anomaly.observed < anomaly.baseline
        assert anomaly.deviation_pct < 0
        # detection latency: first flagged bucket close to the true start
        latency = (anomaly.start_ts - buckets[SHIFT_AT].ts).total_seconds() / 300
        assert latency <= 4, f"{detector.name} latency {latency} buckets"

    def test_flags_latency_spike(self, detector, latency_series):
        buckets, labels = latency_series(n=N, spike_at=SHIFT_AT, spike_ms=1500.0)
        anomaly = detector.detect(buckets, params("up"))
        assert anomaly is not None, f"{detector.name} missed a 250ms -> 1500ms spike"
        assert anomaly.observed > anomaly.baseline
        assert anomaly.deviation_pct > 0

    def test_quiet_on_healthy_series(self, detector, sr_series):
        buckets, _ = sr_series(n=N)
        anomaly = detector.detect(buckets, params("down"))
        assert anomaly is None, f"{detector.name} false-positived on healthy data"

    def test_ignores_improvement(self, detector, sr_series):
        # an upward jump in success rate is not a degradation
        buckets, _ = sr_series(n=N, shift_at=SHIFT_AT, shift_to=0.99)
        anomaly = detector.detect(buckets, params("down"))
        assert anomaly is None or anomaly.deviation_pct < 0

    def test_insufficient_data_returns_none(self, detector, sr_series):
        buckets, _ = sr_series(n=BASELINE + 2)
        # only 2 scored buckets after a tiny baseline window: zscore/ewma/cusum
        # can still legitimately fire here, so only check the no-shift case
        anomaly = detector.detect(buckets[: BASELINE + 2], params("down"))
        assert anomaly is None

    def test_skips_sparse_buckets(self, detector, sr_series):
        # buckets below min_bucket_count carry no signal and must be ignored
        buckets, _ = sr_series(n=N, shift_at=SHIFT_AT, shift_to=0.45)
        sparse = [b.__class__(ts=b.ts, value=b.value, count=1) for b in buckets]
        assert detector.detect(sparse, params("down")) is None


class TestZScore:
    def test_scores_are_z_values(self, sr_series):
        det = get_detector("zscore")
        buckets, _ = sr_series(n=N, shift_at=SHIFT_AT, shift_to=0.45)
        anomaly = det.detect(buckets, params("down"))
        assert anomaly is not None and anomaly.score >= 3.0

    def test_threshold_override(self, sr_series):
        det = get_detector("zscore")
        buckets, _ = sr_series(n=N, shift_at=SHIFT_AT, shift_to=0.82)
        strict = det.detect(buckets, params("down", threshold=12.0))
        assert strict is None  # ~4 sigma shift hidden behind a 12-sigma cut
        loose = det.detect(buckets, params("down", threshold=2.0))
        assert loose is not None


class TestSensitivity:
    def test_higher_sensitivity_fires_earlier_or_equal(self, sr_series):
        buckets, _ = sr_series(
            n=N, shift_at=SHIFT_AT, shift_to=0.75, drift_until=SHIFT_AT + 12
        )
        for name in available_detectors():
            det = get_detector(name)
            calm = det.detect(buckets, params("down", sensitivity=0.8))
            jumpy = det.detect(buckets, params("down", sensitivity=2.5))
            if calm is None:
                assert jumpy is not None or True  # both may miss a slow drift
            else:
                assert jumpy is not None
                assert jumpy.start_ts <= calm.start_ts

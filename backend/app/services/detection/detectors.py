"""Degradation detectors: a Detector protocol, a registry, and four
implementations of increasing sophistication.

Every detector follows the same **baselines-first** contract:

1. The first ``baseline_buckets`` valid buckets of the series are assumed
   healthy and define the baseline (mean/std or training sample).
2. Only buckets after the baseline are scored. Buckets with too few events
   (``min_bucket_count``) or no data are skipped — tiny samples would make
   success-rate estimates swing wildly.
3. Detection is direction-aware: ``down`` (success rate) flags drops,
   ``up`` (latency) flags rises. Opposite-direction outliers are ignored.
4. ``sensitivity`` (> 0, default 1.0) scales every detector's threshold:
   higher sensitivity -> lower effective threshold -> fires earlier and on
   smaller shifts, at the cost of false positives.

Detectors return at most one :class:`Anomaly` — the flagged buckets of the
degradation, summarized by its first evidence (``start_ts``, what drives
detection latency) and its worst bucket (``observed``/``deviation_pct``).
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.services.detection.series import Bucket

# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectorParams:
    """Tunables shared by all detectors.

    ``threshold`` overrides the detector's default decision threshold
    (z-score cut, EWMA L, CUSUM h-in-sigma, IsolationForest contamination);
    ``sensitivity`` then divides it (except contamination, which it scales).
    """

    baseline_buckets: int = 12
    threshold: float | None = None
    sensitivity: float = 1.0
    min_bucket_count: int = 5
    direction: str = "down"  # "down" | "up"
    bucket_minutes: int = 5


@dataclass(frozen=True)
class Anomaly:
    """A detected degradation episode."""

    detector: str
    start_ts: datetime  # first flagged bucket — the estimated degradation start
    end_ts: datetime  # last flagged bucket
    baseline: float  # baseline level the detector compared against
    observed: float  # most degraded bucket value inside the episode
    deviation_pct: float  # signed: negative for a drop, positive for a rise
    score: float  # detector-internal score at the worst bucket
    flagged_ts: tuple[datetime, ...] = ()  # every flagged bucket


class Detector(Protocol):
    """Degradation detector over a bucketed metric series."""

    name: str

    def detect(self, buckets: list[Bucket], params: DetectorParams) -> Anomaly | None: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_points(buckets: list[Bucket], params: DetectorParams) -> list[Bucket]:
    return [
        b
        for b in buckets
        if b.value is not None and b.count >= params.min_bucket_count
    ]


def _baseline_stats(values: list[float]) -> tuple[float, float]:
    """(mean, std) of the baseline. Std gets a relative floor so a nearly
    constant healthy baseline (e.g. success rate pinned at 1.0) does not make
    the detector hair-trigger on trivial wobble."""
    n = len(values)
    mu = sum(values) / n
    if n > 1:
        var = sum((v - mu) ** 2 for v in values) / (n - 1)
    else:
        var = 0.0
    sigma = max(math.sqrt(var), abs(mu) * 0.01, 1e-9)
    return mu, sigma


def _degraded(value: float, reference: float, direction: str) -> bool:
    return value < reference if direction == "down" else value > reference


def _worse(a: float, b: float, direction: str) -> float:
    """Return the more degraded of two values."""
    return min(a, b) if direction == "down" else max(a, b)


def _effective_threshold(params: DetectorParams, default: float) -> float:
    base = params.threshold if params.threshold is not None else default
    return base / max(params.sensitivity, 1e-9)


def _split_baseline(
    points: list[Bucket], params: DetectorParams, min_scored: int = 1
) -> tuple[list[Bucket], list[Bucket]] | None:
    """Split valid points into (baseline, scored). None if there is not
    enough data to say anything."""
    if len(points) < params.baseline_buckets + min_scored:
        return None
    return points[: params.baseline_buckets], points[params.baseline_buckets :]


def _build_anomaly(
    name: str,
    flagged: list[tuple[Bucket, float]],
    baseline: float,
    params: DetectorParams,
) -> Anomaly | None:
    """Summarize the flagged buckets as one anomaly: ``start_ts`` is the FIRST
    bucket with degradation evidence (that is what drives detection latency),
    ``observed``/``deviation_pct``/``score`` describe the worst bucket."""
    if not flagged:
        return None
    worst_bucket, worst_score = _worst(flagged, params.direction)
    ts_list = [b.ts for b, _ in flagged]
    return Anomaly(
        detector=name,
        start_ts=min(ts_list),
        end_ts=max(ts_list),
        baseline=baseline,
        observed=worst_bucket.value,
        deviation_pct=_deviation_pct(worst_bucket.value, baseline),
        score=worst_score,
        flagged_ts=tuple(ts_list),
    )


def _worst(ep: list[tuple[Bucket, float]], direction: str) -> tuple[Bucket, float]:
    worst = ep[0]
    for item in ep[1:]:
        if _degraded(item[0].value, worst[0].value, direction) and item[0].value != worst[0].value:
            worst = item
    return worst


def _deviation_pct(observed: float, baseline: float) -> float:
    if baseline == 0:
        return 0.0 if observed == 0 else math.copysign(100.0, observed)
    return (observed - baseline) / abs(baseline) * 100.0


# ---------------------------------------------------------------------------
# 1. Rolling-baseline z-score
# ---------------------------------------------------------------------------


class RollingZScoreDetector:
    """Flags buckets whose value lies more than ``threshold`` baseline
    standard deviations in the degradation direction from the baseline mean.

    Fastest to fire on sudden step shifts; weakest on slow drifts (each
    individual bucket may stay under the cut).
    """

    name = "zscore"
    default_threshold = 3.0

    def detect(self, buckets: list[Bucket], params: DetectorParams) -> Anomaly | None:
        points = _valid_points(buckets, params)
        split = _split_baseline(points, params)
        if split is None:
            return None
        baseline_pts, scored = split
        mu, sigma = _baseline_stats([b.value for b in baseline_pts])
        thr = _effective_threshold(params, self.default_threshold)

        flagged: list[tuple[Bucket, float]] = []
        for b in scored:
            z = (mu - b.value) / sigma if params.direction == "down" else (b.value - mu) / sigma
            if z >= thr:
                flagged.append((b, z))
        return _build_anomaly(self.name, flagged, mu, params)


# ---------------------------------------------------------------------------
# 2. EWMA control chart
# ---------------------------------------------------------------------------


class EWMADetector:
    """Exponentially weighted moving average control chart.

    Tracks S_t = lam*x_t + (1-lam)*S_{t-1} and flags when S_t leaves the
    time-varying control limit L * sigma * sqrt(lam/(2-lam) * (1-(1-lam)^2t)).
    Smooths noise, so it catches smaller sustained shifts than z-score with
    a bucket or two of extra lag.
    """

    name = "ewma"
    default_lam = 0.3
    default_L = 3.0

    def detect(self, buckets: list[Bucket], params: DetectorParams) -> Anomaly | None:
        points = _valid_points(buckets, params)
        split = _split_baseline(points, params)
        if split is None:
            return None
        baseline_pts, scored = split
        mu, sigma = _baseline_stats([b.value for b in baseline_pts])
        lam = self.default_lam
        limit = _effective_threshold(params, self.default_L)

        s = mu
        flagged: list[tuple[Bucket, float]] = []
        for t, b in enumerate(scored, start=1):
            s = lam * b.value + (1 - lam) * s
            sigma_s = sigma * math.sqrt(lam / (2 - lam) * (1 - (1 - lam) ** (2 * t)))
            z = (mu - s) / max(sigma_s, 1e-9) if params.direction == "down" else (s - mu) / max(sigma_s, 1e-9)
            if z >= limit:
                flagged.append((b, z))
        return _build_anomaly(self.name, flagged, mu, params)


# ---------------------------------------------------------------------------
# 3. CUSUM
# ---------------------------------------------------------------------------


class CUSUMDetector:
    """One-sided cumulative sum chart in the degradation direction.

    Accumulates evidence S_t = max(0, S_{t-1} + deviation - k) with allowance
    k = 0.5*sigma; flags when S_t exceeds h = threshold*sigma. The statistic
    is capped at h + k so the alarm clears within a bucket or two once the
    series recovers (uncapped CUSUM would overhang for the rest of the
    window). The classic change-point detector: best mean-delay trade-off for
    small persistent shifts, slightly slower than z-score on huge steps.
    """

    name = "cusum"
    default_threshold = 4.0  # h, in baseline sigmas
    allowance_sigma = 0.5  # k, in baseline sigmas

    def detect(self, buckets: list[Bucket], params: DetectorParams) -> Anomaly | None:
        points = _valid_points(buckets, params)
        split = _split_baseline(points, params)
        if split is None:
            return None
        baseline_pts, scored = split
        mu, sigma = _baseline_stats([b.value for b in baseline_pts])
        h = _effective_threshold(params, self.default_threshold) * sigma
        k = self.allowance_sigma * sigma
        cap = h + k  # bound the statistic so the alarm clears after recovery

        s = 0.0
        flagged: list[tuple[Bucket, float]] = []
        for b in scored:
            dev = (mu - b.value) if params.direction == "down" else (b.value - mu)
            s = min(max(0.0, s + dev - k), cap)
            if s > h:
                flagged.append((b, s / sigma))
        return _build_anomaly(self.name, flagged, mu, params)


# ---------------------------------------------------------------------------
# 4. IsolationForest (sklearn) — baseline-trained novelty detection
# ---------------------------------------------------------------------------


class IsolationForestDetector:
    """Multivariate outlier detector over per-bucket features (sklearn).

    Features per bucket: the value, its first difference, and its deviation
    from the trailing 3-bucket mean — so level shifts, slope changes, and
    sawtooth oscillation all look anomalous. The forest is fitted over the
    whole window (classic mode: with tiny baselines, novelty-mode offsets
    degenerate because a handful of training points self-isolate), then three
    gates align it with degradation semantics:

    1. only post-baseline buckets are reported;
    2. direction: the bucket must be degraded vs the baseline median
       (an unexpected *improvement* is not an incident);
    3. magnitude: deviation must exceed ``gate_sigma`` baseline stddevs, so
       healthy-series noise outliers don't fire.
    """

    name = "isolation_forest"
    default_contamination = 0.25
    min_baseline_points = 8
    gate_sigma = 2.5

    def detect(self, buckets: list[Bucket], params: DetectorParams) -> Anomaly | None:
        try:
            import numpy as np
            from sklearn.ensemble import IsolationForest
        except ImportError:  # pragma: no cover - sklearn is a pinned dep
            return None

        points = _valid_points(buckets, params)
        if len(points) < max(params.baseline_buckets, self.min_baseline_points) + 1:
            return None
        n_base = params.baseline_buckets
        baseline_pts, scored = points[:n_base], points[n_base:]
        if len(baseline_pts) < self.min_baseline_points or not scored:
            return None

        values = [b.value for b in points]
        base_vals = values[:n_base]
        sorted_base = sorted(base_vals)
        median_base = (
            sorted_base[n_base // 2]
            if n_base % 2
            else (sorted_base[n_base // 2 - 1] + sorted_base[n_base // 2]) / 2
        )
        _, sigma = _baseline_stats(base_vals)

        if params.threshold is not None:
            contamination = min(max(params.threshold, 0.01), 0.5)
        else:
            contamination = min(
                max(self.default_contamination * params.sensitivity, 0.02), 0.25
            )
        forest = IsolationForest(
            n_estimators=100,
            contamination=contamination,
            random_state=42,
        )
        features = np.array(_features(values), dtype=float)
        preds = forest.fit_predict(features)  # -1 = outlier
        scores = -forest.score_samples(features)  # higher = more anomalous

        gate = self.gate_sigma * sigma
        flagged: list[tuple[Bucket, float]] = []
        for idx, b in enumerate(scored, start=n_base):
            if preds[idx] != -1:
                continue
            dev = (
                (median_base - b.value)
                if params.direction == "down"
                else (b.value - median_base)
            )
            if dev >= gate:
                flagged.append((b, float(scores[idx])))
        return _build_anomaly(self.name, flagged, median_base, params)


def _features(values: list[float]) -> list[list[float]]:
    feats: list[list[float]] = []
    for i, v in enumerate(values):
        lag_diff = v - values[i - 1] if i > 0 else 0.0
        if i >= 3:
            trailing = sum(values[i - 3 : i]) / 3
        else:
            trailing = sum(values[: i + 1]) / (i + 1)
        feats.append([v, lag_diff, v - trailing])
    return feats


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Callable[[], Detector]] = {
    RollingZScoreDetector.name: RollingZScoreDetector,
    EWMADetector.name: EWMADetector,
    CUSUMDetector.name: CUSUMDetector,
    IsolationForestDetector.name: IsolationForestDetector,
}


def available_detectors() -> list[str]:
    return sorted(_REGISTRY)


def get_detector(name: str) -> Detector:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"unknown detector: {name!r} (available: {', '.join(available_detectors())})"
        ) from None


def all_detectors() -> list[Detector]:
    return [factory() for factory in _REGISTRY.values()]


__all__ = [
    "Anomaly",
    "Detector",
    "DetectorParams",
    "RollingZScoreDetector",
    "EWMADetector",
    "CUSUMDetector",
    "IsolationForestDetector",
    "available_detectors",
    "get_detector",
    "all_detectors",
]

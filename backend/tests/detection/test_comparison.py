"""Detector comparison on labeled synthetic series: precision, recall, and
detection latency (buckets from the true shift start to the first flagged
bucket) per detector, aggregated over a small scenario suite.

IMPORTANT: these are SYNTHETIC-FIXTURE results on tiny seeded series — they
exist to prove the detectors behave as designed and to give the evaluation
agent a sanity baseline. They are NOT production accuracy numbers.
"""

import pytest

from app.services.detection.detectors import (
    DetectorParams,
    all_detectors,
)

BASELINE = 8
PARAMS_DOWN = DetectorParams(
    baseline_buckets=BASELINE, direction="down", bucket_minutes=5, min_bucket_count=5
)
PARAMS_UP = DetectorParams(
    baseline_buckets=BASELINE, direction="up", bucket_minutes=5, min_bucket_count=5
)


def _scenarios(sr_series, latency_series):
    """(name, buckets, labels, params) — labels are the truly degraded bucket
    indices; empty labels = healthy control."""
    return [
        (
            "sharp_sr_drop",
            *sr_series(n=40, shift_at=20, shift_to=0.45, seed=1),
            PARAMS_DOWN,
        ),
        (
            "moderate_sr_drop",
            *sr_series(n=40, shift_at=20, shift_to=0.70, seed=2),
            PARAMS_DOWN,
        ),
        (
            "gradual_sr_drift",
            *sr_series(n=40, shift_at=20, shift_to=0.60, drift_until=32, seed=3),
            PARAMS_DOWN,
        ),
        (
            "sr_drop_with_recovery",
            *sr_series(n=40, shift_at=20, shift_to=0.50, recover_at=30, seed=4),
            PARAMS_DOWN,
        ),
        (
            "latency_spike",
            *latency_series(n=40, spike_at=20, spike_ms=1500.0, seed=5),
            PARAMS_UP,
        ),
        (
            "healthy_control",
            *sr_series(n=40, seed=6),
            PARAMS_DOWN,
        ),
    ]


def _evaluate(detector, scenarios):
    tp = fp = fn = labeled_total = 0
    latencies: list[float] = []
    per_scenario = []
    for name, buckets, labels, params in scenarios:
        anomaly = detector.detect(buckets, params)
        flagged = {i for i, b in enumerate(buckets) if anomaly and b.ts in anomaly.flagged_ts}
        s_tp = len(flagged & labels)
        s_fp = len(flagged - labels)
        s_fn = len(labels - flagged)
        tp += s_tp
        fp += s_fp
        fn += s_fn
        labeled_total += len(labels)
        latency = None
        if labels and flagged & labels:
            # delay from the true shift start to the first flagged bucket that
            # is genuinely degraded (early false alarms don't count as detection)
            latency = float(min(flagged & labels) - min(labels))
            latencies.append(latency)
        per_scenario.append(
            {
                "scenario": name,
                "detected": bool(flagged & labels) if labels else bool(flagged),
                "tp": s_tp,
                "fp": s_fp,
                "fn": s_fn,
                "latency_buckets": latency,
            }
        )
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    mean_latency = sum(latencies) / len(latencies) if latencies else None
    return {
        "detector": detector.name,
        "precision": precision,
        "recall": recall,
        "mean_detection_latency_buckets": mean_latency,
        "false_positive_buckets": fp,
        "scenarios": per_scenario,
    }


def test_detector_comparison(sr_series, latency_series, capsys):
    scenarios = _scenarios(sr_series, latency_series)
    results = [_evaluate(det, scenarios) for det in all_detectors()]

    header = f"{'detector':<18} {'precision':>9} {'recall':>7} {'MTTD(buckets)':>14} {'FP buckets':>10}"
    lines = [header, "-" * len(header)]
    for r in results:
        latency = (
            f"{r['mean_detection_latency_buckets']:.1f}"
            if r["mean_detection_latency_buckets"] is not None
            else "n/a"
        )
        lines.append(
            f"{r['detector']:<18} {r['precision']:>9.2f} {r['recall']:>7.2f} "
            f"{latency:>14} {r['false_positive_buckets']:>10}"
        )
    table = "\n".join(lines)
    print("\nDETECTOR COMPARISON (synthetic fixtures — preliminary, not production metrics)")
    print(table)

    for r in results:
        # Per-detector floors, calibrated to each algorithm's design (see
        # docs/detection.md): isolation_forest deliberately flags only the
        # most anomalous boundary buckets, so its per-bucket recall is lower
        # by design; the charts track the whole degraded region.
        recall_floor = 0.3 if r["detector"] == "isolation_forest" else 0.5
        assert r["recall"] >= recall_floor, f"{r['detector']} recall too low: {r['recall']:.2f}"
        assert r["precision"] >= 0.5, f"{r['detector']} precision too low: {r['precision']:.2f}"
        assert (
            r["mean_detection_latency_buckets"] is not None
            and r["mean_detection_latency_buckets"] <= 6
        ), f"{r['detector']} too slow: {r['mean_detection_latency_buckets']}"
        # healthy control must not produce more than incidental false positives
        control = next(s for s in r["scenarios"] if s["scenario"] == "healthy_control")
        assert control["fp"] <= 2, f"{r['detector']} noisy on healthy control"


def test_sharp_drop_detected_by_all_with_low_latency(sr_series):
    buckets, labels = sr_series(n=40, shift_at=20, shift_to=0.45, seed=9)
    for det in all_detectors():
        anomaly = det.detect(buckets, PARAMS_DOWN)
        assert anomaly is not None, det.name
        flagged = {i for i, b in enumerate(buckets) if b.ts in anomaly.flagged_ts}
        first_hit = min(flagged & labels)
        assert first_hit - 20 <= 3, f"{det.name} latency {first_hit - 20} buckets on sharp drop"

"""Detection service: degradation detection over the payment_events stream.

Public surface: ``run_detection`` (one detection pass with idempotent incident
persistence), the ``Detector`` protocol + registry, and the series builders
used by both the engine and the tests.
"""

from app.services.detection.detectors import (
    Anomaly,
    CUSUMDetector,
    Detector,
    DetectorParams,
    EWMADetector,
    IsolationForestDetector,
    RollingZScoreDetector,
    all_detectors,
    available_detectors,
    get_detector,
)
from app.services.detection.engine import (
    DetectionRunResult,
    IncidentReport,
    localize,
    run_detection,
    severity_for_deviation,
)
from app.services.detection.series import (
    KNOWN_METRICS,
    METRIC_CAPTURE_LATENCY,
    METRIC_CHECKOUT_ABANDONMENT,
    METRIC_INSUFFICIENT_FUND_SHARE,
    METRIC_SUCCESS_RATE,
    SEGMENT_DIMENSIONS,
    Bucket,
    CheckoutAttempt,
    PaymentOutcome,
    build_abandonment_series,
    build_metric_series,
    build_series,
    floor_bucket,
    load_checkout_attempts,
    load_outcomes,
    slice_outcomes,
)

__all__ = [
    "Anomaly",
    "Bucket",
    "CUSUMDetector",
    "CheckoutAttempt",
    "DetectionRunResult",
    "Detector",
    "DetectorParams",
    "EWMADetector",
    "IncidentReport",
    "IsolationForestDetector",
    "KNOWN_METRICS",
    "METRIC_CAPTURE_LATENCY",
    "METRIC_CHECKOUT_ABANDONMENT",
    "METRIC_INSUFFICIENT_FUND_SHARE",
    "METRIC_SUCCESS_RATE",
    "PaymentOutcome",
    "RollingZScoreDetector",
    "SEGMENT_DIMENSIONS",
    "all_detectors",
    "available_detectors",
    "build_abandonment_series",
    "build_metric_series",
    "build_series",
    "floor_bucket",
    "get_detector",
    "load_checkout_attempts",
    "load_outcomes",
    "localize",
    "run_detection",
    "severity_for_deviation",
    "slice_outcomes",
]

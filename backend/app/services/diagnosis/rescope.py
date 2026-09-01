"""Window re-scoping triage before diagnosis (docs/ml.md §8 — window dilution).

Scheduled detection passes score wide frames (12h lookback), so the incident
window handed to :class:`DiagnosisService` is the DILUTED detection frame,
not the anomalous span. Features computed over the diluted frame wash out
exactly the signatures the diagnosis model keys on (its exact-span top-1
holds only on matched tight windows). This module tightens the frame BEFORE
feature computation: it rebuilds the incident's own metric series with the
detection helpers (same loaders, same bucket grid, same floor constants —
no stats re-implemented here) and keeps the contiguous span of buckets that
actually breach the metric's floors.

The step is a config knob — ``DiagnosisService(rescope_windows=...)`` or the
``DIAGNOSIS_WINDOW_RESCOPE`` env var — DEFAULT OFF, because the published
evaluation anchors (docs/evaluation.md §3/§3b) were measured on the
as-detected frames; flipping the default would silently move them.

Honesty: both frames are recorded on the companion ``model_predictions`` row
(``output["window"]``) and, when the frame changed, on the diagnosis
explanation. The incident's own window is never mutated.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import source_types_for_environment
from app.services.detection.engine import (
    ABANDONMENT_INACTIVITY_MINUTES,
    DEFAULT_MIN_ABSOLUTE_DEVIATION,
    METRIC_BUCKET_MULTIPLIER,
    METRIC_MIN_BUCKET_COUNT,
    METRIC_MIN_OBSERVED,
)
from app.services.detection.series import (
    ATTEMPT_BASED_METRICS,
    KNOWN_METRICS,
    METRIC_DIRECTION,
    Bucket,
    build_metric_series,
    floor_bucket,
    load_checkout_attempts,
    load_outcomes,
)
from app.services.diagnosis.features import incident_windows

logger = logging.getLogger(__name__)

#: Env-var form of the knob (constructor argument wins). Anything outside
#: this set is OFF — the historical, anchor-preserving behavior.
ENV_RESCOPE = "DIAGNOSIS_WINDOW_RESCOPE"

#: Request-side detection defaults (app/schemas/detection.py): the rescope
#: grid/floors mirror a production pass when the incident carries no meta.
_DEFAULT_BUCKET_MINUTES = 5
_DEFAULT_MIN_BUCKET_COUNT = 5


@dataclass(frozen=True)
class RescopedWindow:
    """The triage outcome: the frame diagnosis will score, plus the original
    detection frame it derived from. ``applied`` is False (with ``reason``)
    whenever the scored frame is the original one."""

    original_start: datetime
    original_end: datetime
    scored_start: datetime
    scored_end: datetime
    applied: bool
    reason: str  # disabled | breach_span | meta_anomaly_span | already_tight | no_breach | no_baseline | unknown_metric | unknown_environment | error

    def as_dict(self) -> dict[str, Any]:
        return {
            "original_start": self.original_start.isoformat(),
            "original_end": self.original_end.isoformat(),
            "scored_start": self.scored_start.isoformat(),
            "scored_end": self.scored_end.isoformat(),
            "applied": self.applied,
            "reason": self.reason,
        }


def rescope_enabled(override: bool | None = None) -> bool:
    """Resolve the knob: explicit constructor argument, else the env var."""
    if override is not None:
        return bool(override)
    return os.environ.get(ENV_RESCOPE, "").strip().lower() in {"1", "true", "yes", "on"}


def _passthrough(start: datetime, end: datetime, reason: str) -> RescopedWindow:
    return RescopedWindow(start, end, start, end, False, reason)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _effective_bucket_minutes(incident: Any, metric: str) -> int:
    """The grid the detection pass used for this series (persisted in meta —
    already multiplied per metric), else the request-default grid."""
    raw = (incident.meta or {}).get("bucket_minutes")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return _DEFAULT_BUCKET_MINUTES * METRIC_BUCKET_MULTIPLIER.get(metric, 1)


def _baseline_reference(incident: Any, baseline_buckets: list[Bucket], min_count: int) -> float | None:
    """Reference level for the breach test: the pass's own persisted baseline
    when the incident carries one, else the count-weighted mean of the valid
    pre-window buckets (manual incidents without detection provenance)."""
    if incident.baseline_value is not None:
        return float(incident.baseline_value)
    valid = [b for b in baseline_buckets if b.value is not None and b.count >= min_count]
    if not valid:
        return None
    total = sum(b.count for b in valid)
    return sum(float(b.value) * b.count for b in valid) / total  # type: ignore[operator]


def _breaches(bucket: Bucket, baseline: float, metric: str, min_count: int) -> bool:
    """The engine's floor rule applied per bucket: skip statistically empty
    buckets, then test the metric's absolute-deviation floor in the metric's
    degraded direction, plus the observed-level bar for share metrics."""
    if bucket.value is None or bucket.count < min_count:
        return False
    floor = DEFAULT_MIN_ABSOLUTE_DEVIATION[metric]
    if METRIC_DIRECTION[metric] == "down":
        breached = bucket.value <= baseline - floor
    else:
        breached = bucket.value >= baseline + floor
    if not breached:
        return False
    min_observed = METRIC_MIN_OBSERVED.get(metric)
    return min_observed is None or bucket.value >= min_observed


def _meta_span_fallback(incident: Any, w_start: datetime, w_end: datetime) -> RescopedWindow:
    """No recomputed breach — trust the detector's persisted anomaly span
    (``meta.anomaly_start``/``anomaly_end``) when it is strictly tighter."""
    meta = incident.meta or {}
    start, end = _parse_iso(meta.get("anomaly_start")), _parse_iso(meta.get("anomaly_end"))
    if start is not None and end is not None:
        s, e = max(start, w_start), min(end, w_end)
        if s < e and (e - s) < (w_end - w_start):
            return RescopedWindow(w_start, w_end, s, e, True, "meta_anomaly_span")
    return _passthrough(w_start, w_end, "no_breach")


def rescope_incident_window(session: Session, incident: Any) -> RescopedWindow:
    """Tighten an incident's detection window to the anomalous span.

    Rebuilds the incident's metric series over ``[window - duration, window]``
    on the pass's own bucket grid — environment-scoped (the real_test /
    research boundary) and segment-sliced exactly like the detection pass —
    then keeps the span from the first to the last floor-breaching bucket.
    Any failure to prove a tighter span passes the original frame through
    unchanged; the tight span is always clamped inside the original window.
    """
    _, _, w_start, w_end = incident_windows(incident)  # ValueError on a bad window — caller's contract
    metric = incident.metric
    if metric not in KNOWN_METRICS:
        return _passthrough(w_start, w_end, "unknown_metric")
    try:
        source_types = source_types_for_environment(incident.environment)
    except ValueError:
        return _passthrough(w_start, w_end, "unknown_environment")

    segment = (incident.meta or {}).get("segment") or {}
    bucket_minutes = _effective_bucket_minutes(incident, metric)
    duration = w_end - w_start
    ext_start = w_start - duration

    if metric in ATTEMPT_BASED_METRICS:
        records: list = load_checkout_attempts(
            session,
            ext_start,
            w_end,
            segment,
            inactivity_minutes=ABANDONMENT_INACTIVITY_MINUTES,
            source_types=source_types,
        )
    else:
        records = load_outcomes(session, ext_start, w_end, segment, source_types)
    series = build_metric_series(
        records,
        metric=metric,
        window_start=ext_start,
        window_end=w_end,
        bucket_minutes=bucket_minutes,
    )

    grid_start = floor_bucket(w_start, bucket_minutes)
    min_count = METRIC_MIN_BUCKET_COUNT.get(metric, _DEFAULT_MIN_BUCKET_COUNT)
    baseline_buckets = [b for b in series if b.ts < grid_start]
    window_buckets = [b for b in series if grid_start <= b.ts < w_end]
    baseline = _baseline_reference(incident, baseline_buckets, min_count)
    if baseline is None:
        return _passthrough(w_start, w_end, "no_baseline")

    breaching = [b for b in window_buckets if _breaches(b, baseline, metric, min_count)]
    if not breaching:
        return _meta_span_fallback(incident, w_start, w_end)

    step = timedelta(minutes=bucket_minutes)
    tight_start = max(min(b.ts for b in breaching), w_start)
    tight_end = min(max(b.ts for b in breaching) + step, w_end)
    if not tight_start < tight_end or (tight_end - tight_start) >= duration:
        return _passthrough(w_start, w_end, "already_tight")
    return RescopedWindow(w_start, w_end, tight_start, tight_end, True, "breach_span")


__all__ = ["ENV_RESCOPE", "RescopedWindow", "rescope_enabled", "rescope_incident_window"]

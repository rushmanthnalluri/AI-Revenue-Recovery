"""Detection engine: one detection pass over the payment_events stream.

A pass (``run_detection``) does:

1. Resolve the analysis window — anchored at the latest terminal event (or an
   explicit ``as_of``) so identical data yields an identical window.
2. Build the bucketed series per metric and run the chosen detector(s).
   Four metrics: ``payment_success_rate`` and ``capture_latency_ms`` over
   terminal outcomes, ``checkout_abandonment_rate`` over checkout *attempts*
   (payments stuck in ``created``; outcome-based series are blind to them),
   and ``insufficient_fund_share`` (the insufficient-funds mix of failures,
   built for the small-volume night regime). Sparse signals use coarser
   per-metric bucket grids (``METRIC_BUCKET_MULTIPLIER``).
3. Apply the incident-level noise floors (Watchdog-style): a detector fire
   becomes an incident only when the deviation is big in absolute terms
   (``min_absolute_deviation``), touches enough traffic
   (``min_flagged_volume``), and persists across enough consecutive buckets
   (``min_flagged_run``). Small-volume metrics carry their own floor defaults
   (applied unless the request sets the floor explicitly). Fires that fail a
   floor are counted (``anomalies_filtered``) and dropped — organic
   night-traffic wobble is not an incident. An opt-in night-regime floor set
   (``night_regime_floors``, default OFF) judges all-night
   ``insufficient_fund_share`` anomalies by a lower share/absolute bar —
   the global 0.90-share bar exists for organic DAYTIME clusters only.
4. Blind-spot cover: when the merchant-wide latency pass admits nothing for a
   detector, re-score per-route latency slices (``_scan_latency_routes``) —
   a single route's latency collapse is invisible in the aggregate series
   (sparse buckets poison the leading baseline) but stark in its own series.
5. Localize each surviving anomaly by re-scoring per-segment slices
   (method / bank / gateway / route) and ranking contributors by deviation.
6. Persist: one ``incidents`` row per (metric, detector, window, segment) —
   re-running the same combination UPDATEs that row (original ``detected_at``
   preserved, evidence refreshed) instead of duplicating it. Cross-window
   re-detection of the SAME episode (overlapping scheduled passes) is merged
   into the open incident when the anomaly spans overlap or lie within
   ``dedup_cooldown_minutes``; re-detection of a signature that was resolved
   (RESOLVED/CLOSED/FALSE_POSITIVE) within ``suppress_after_resolve_minutes``
   is suppressed, not reopened.
7. Attach ``incident_evidence``: a ``metric_series`` snapshot and the
   ``segment_breakdown`` ranking.

Detection latency is computable from the persisted record: the true start
comes from simulator ground truth, and ``meta.anomaly_start`` /
``incidents.detected_at`` give the estimated start and detection time.
"""

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import ids
from app.db import utcnow
from app.logging import get_logger
from app.models import Incident, IncidentEvidence, source_types_for_environment
from app.ports import IncidentStatus, Severity
from app.schemas.detection import DetectionRunRequest
from app.services.detection.detectors import (
    Anomaly,
    DetectorParams,
    all_detectors,
    get_detector,
)
from app.services.detection.series import (
    ATTEMPT_BASED_METRICS,
    SEGMENT_DIMENSIONS,
    KNOWN_METRICS,
    METRIC_CAPTURE_LATENCY,
    METRIC_CHECKOUT_ABANDONMENT,
    METRIC_DIRECTION,
    METRIC_INSUFFICIENT_FUND_SHARE,
    METRIC_SUCCESS_RATE,
    UNKNOWN_SEGMENT,
    Bucket,
    PaymentOutcome,
    build_metric_series,
    build_series,
    floor_bucket,
    is_insufficient_fund,
    latest_event_anchor,
    load_checkout_attempts,
    load_outcomes,
    slice_outcomes,
)

logger = get_logger("app.services.detection")

COLLECTOR = "agent:detection"
TOP_SEGMENTS_PER_DIMENSION = 3

#: Incident-level floor: minimum absolute |observed - baseline| per metric, in
#: metric-native units (request-overridable via ``min_absolute_deviation``).
#: 5 percentage points for success rate, 75 ms for capture latency — below
#: that, a detector fire is organic wobble, not an incident. Measured on the
#: standard-scenario harness: not the binding floor there (organic noise
#: deviates far more), it guards quiet-merchant hair-triggers.
#: The two share-metrics get wider floors (10pp stuck-share, 25pp error-share)
#: because small-denominator shares wobble further than rates.
DEFAULT_MIN_ABSOLUTE_DEVIATION: dict[str, float] = {
    METRIC_SUCCESS_RATE: 0.05,
    METRIC_CAPTURE_LATENCY: 75.0,
    METRIC_CHECKOUT_ABANDONMENT: 0.20,
    METRIC_INSUFFICIENT_FUND_SHARE: 0.25,
}

#: Admission floor on the observed level itself (up-direction share metrics
#: only): a *wave* means the failure mix is DOMINATED by the signal class,
#: not merely elevated. Measured on standard/seed42 (30 days): organic
#: insufficient-fund clusters peak at 0.71 share (daytime, 7 failures, z up
#: to 7) while the injected wave's night bucket is 1.0 — the 0.9 bar sits in
#: the measured gap between them; natural checkout abandonment clumps at
#: <= 0.2 share on decidable buckets while the injected spike runs 0.4-0.8.
METRIC_MIN_OBSERVED: dict[str, float] = {
    METRIC_CHECKOUT_ABANDONMENT: 0.35,
    METRIC_INSUFFICIENT_FUND_SHARE: 0.90,
}

#: Per-metric bucket-size multiplier relative to ``req.bucket_minutes``:
#: sparse signals need coarser buckets to carry statistical content at all.
#: Measured on the standard harness (seed 42): the abandonment spike is clean
#: on 30-min buckets (baseline ~0.04 vs spike ~0.55), and the insufficient-
#: funds wave's night band only becomes decidable on 60-min buckets (1-3
#: failures per 30-min bucket cannot form a scored run).
METRIC_BUCKET_MULTIPLIER: dict[str, int] = {
    METRIC_CHECKOUT_ABANDONMENT: 6,  # 30-min buckets on the 5-min grid
    METRIC_INSUFFICIENT_FUND_SHARE: 12,  # 60-min buckets on the 5-min grid
}

#: Per-metric floor defaults, applied only when the request does NOT set the
#: floor explicitly (``model_fields_set``). The insufficient-funds wave runs
#: at night, where buckets carry 1-3 failures: the global floors
#: (min_bucket_count 5, min_flagged_volume 15 events) can never be met there —
#: that IS the measured blind spot (see ml/experiments/detection/exp003).
#: The metric therefore scores buckets with >= 2 failures and admits a
#: single-bucket episode carrying >= 3 failures — but only when the hour is
#: near-pure single-class (``METRIC_MIN_OBSERVED``), because measured organic
#: daytime IF clusters reach 0.71 share on 7 failures (z up to 7): a weaker
#: bar fires on organic noise, a stronger one misses the wave.
METRIC_MIN_BUCKET_COUNT: dict[str, int] = {
    METRIC_INSUFFICIENT_FUND_SHARE: 2,
    # decidable creations per 30-min bucket: natural abandonment (a few %)
    # only ever strands 1-2 payments in a bucket — on >= 10 decidable
    # creations that is a <= 0.2 share, under this metric's floors.
    METRIC_CHECKOUT_ABANDONMENT: 10,
}
METRIC_MIN_FLAGGED_RUN: dict[str, int] = {METRIC_INSUFFICIENT_FUND_SHARE: 1}
METRIC_MIN_FLAGGED_VOLUME: dict[str, int] = {METRIC_INSUFFICIENT_FUND_SHARE: 3}

#: Opt-in night-regime floor set for the insufficient-funds wave (request
#: knob ``night_regime_floors``, default OFF — every published anchor in
#: docs/evaluation.md §3b was measured with the single global floor set, and
#: stays valid because the mode ships dark). The measured mechanism (§3b
#: note 2): the wave's failures spread across night-trough buckets and never
#: concentrate into the near-single-class hour the global 0.90
#: ``METRIC_MIN_OBSERVED`` bar demands, so the kind goes 0/7. That bar exists
#: only because organic DAYTIME clusters reach 0.71 share — at night the mix
#: has no such competitor, so an all-night anomaly can be judged by a lower
#: bar. The night band is an engine-fixed UTC approximation of the IST night
#: trough (00:00–06:30 IST ≈ 18:00–01:00 UTC); everything here is UTC, no tz
#: database. The lower set applies ONLY when every flagged bucket sits in the
#: band — a mixed day/night anomaly faces the global floors. Values chosen
#: from the published measurements (wave night hours dilute under 0.90, and
#: an elevated organic IF baseline shrinks |observed-baseline|), disclosed —
#: not re-tuned on the anchor suite.
NIGHT_REGIME_START_HOUR = 18  # 18:00 UTC = 23:30 IST
NIGHT_REGIME_END_HOUR = 1  # band end (exclusive): 01:00 UTC = 06:30 IST
NIGHT_MIN_OBSERVED: dict[str, float] = {METRIC_INSUFFICIENT_FUND_SHARE: 0.60}
NIGHT_MIN_ABSOLUTE_DEVIATION: dict[str, float] = {METRIC_INSUFFICIENT_FUND_SHARE: 0.15}

#: Inactivity threshold for the checkout-abandonment signal: a payment created
#: this many minutes ago without a terminal outcome is abandoned (Razorpay
#: checkout sessions expire on this order of magnitude).
ABANDONMENT_INACTIVITY_MINUTES = 30

#: Route-localized latency scan (the route_latency blind spot): when a pass
#: admits no merchant-wide ``capture_latency_ms`` incident for a detector,
#: re-run that detector on per-route latency slices. Slice series use coarser
#: buckets and a lower count floor than the aggregate (a single route carries
#: a fraction of traffic; 5-min slice buckets at night hold 1-4 captures).
#: The scan only runs when the aggregate is silent, so fleet-wide latency
#: incidents (gateway_degradation) never produce duplicate slice incidents.
LATENCY_ROUTE_SCAN_ENABLED = True
LATENCY_SCAN_DIMENSION = "route"
LATENCY_SCAN_BUCKET_MULTIPLIER = 3  # 15-min slice buckets on the 5-min grid
LATENCY_SCAN_MIN_BUCKET_COUNT = 3
#: A "slice" covering >= 95% of outcomes localizes nothing (it IS the
#: aggregate — e.g. every payment missing the route tag); never scan it.
LATENCY_SCAN_MAX_SLICE_SHARE = 0.95

#: Opt-in ``same_time_yesterday`` baseline mode (request knob, default off):
#: the detector baseline becomes the SAME clock window shifted back this far,
#: so daily seasonality compares against yesterday's own hours instead of the
#: analysis window's leading buckets. The mode needs at least this many
#: decidable baseline (yesterday) buckets per metric — mirroring the request
#: schema's ge=4 floor for leading-window baselines — or the metric stays
#: silent for the pass (an honest no-data, never a guessed baseline).
SAME_TIME_YESTERDAY_SHIFT = timedelta(hours=24)
STY_MIN_BASELINE_BUCKETS = 4

#: Statuses after which re-detection of the same signature is eligible for
#: the post-resolution suppression window.
TERMINAL_STATUSES: tuple[IncidentStatus, ...] = (
    IncidentStatus.RESOLVED,
    IncidentStatus.CLOSED,
    IncidentStatus.FALSE_POSITIVE,
)


@dataclass(frozen=True)
class IncidentReport:
    """One created/updated (or, on dry-run, hypothetical) incident."""

    incident_id: str | None
    action: str  # "created" | "updated" | "would_create" | "would_update" | "suppressed"
    metric: str
    detector: str
    severity: Severity
    baseline_value: float
    observed_value: float
    deviation_pct: float
    segment: dict[str, str]
    window_start: datetime
    window_end: datetime
    detected_at: datetime | None
    anomaly_start: datetime
    affected_payments_count: int
    revenue_at_risk_paise: int
    currency: str = "INR"
    detail: str | None = None  # e.g. merge note / suppression reason


@dataclass
class DetectionRunResult:
    run_id: str
    status: str  # "completed" | "failed"
    started_at: datetime
    finished_at: datetime | None = None
    anomalies_detected: int = 0
    anomalies_filtered: int = 0  # detector fires dropped by floors/suppression
    incidents_created: list[str] = field(default_factory=list)
    incidents_updated: list[str] = field(default_factory=list)
    incidents: list[IncidentReport] = field(default_factory=list)
    detail: str | None = None


def severity_for_deviation(deviation_pct: float) -> Severity:
    """Map absolute deviation magnitude to severity."""
    magnitude = abs(deviation_pct)
    if magnitude >= 50:
        return Severity.CRITICAL
    if magnitude >= 25:
        return Severity.HIGH
    if magnitude >= 10:
        return Severity.MEDIUM
    return Severity.LOW


def run_detection(db: Session, req: DetectionRunRequest) -> DetectionRunResult:
    started_at = utcnow()
    run_id = ids.new_id("det_")

    metrics = req.metrics or list(KNOWN_METRICS)
    unknown_metrics = [m for m in metrics if m not in KNOWN_METRICS]
    if unknown_metrics:
        raise ValueError(
            f"unknown metrics: {unknown_metrics} (known: {', '.join(KNOWN_METRICS)})"
        )
    detectors = all_detectors() if req.detector == "all" else [get_detector(req.detector)]

    # Environment boundary: the pass scores ONLY this environment's commerce
    # rows and stamps it onto every incident/evidence it persists.
    environment = req.environment
    source_types = source_types_for_environment(environment)

    result = DetectionRunResult(run_id=run_id, status="completed", started_at=started_at)

    anchor = req.as_of or latest_event_anchor(db, source_types=source_types)
    if anchor is None:
        result.finished_at = utcnow()
        result.detail = "no terminal payment events in scope; nothing to detect"
        return result
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)

    # Aligned, bucket-grid window: [window_start, window_end).
    window_end = floor_bucket(anchor, req.bucket_minutes) + timedelta(minutes=req.bucket_minutes)
    window_start = window_end - timedelta(minutes=req.window_minutes)
    # floor the start onto the same grid so repeated runs are identical
    window_start = floor_bucket(window_start, req.bucket_minutes)

    outcomes = load_outcomes(db, window_start, window_end, req.segment, source_types)
    if not outcomes and not any(m in ATTEMPT_BASED_METRICS for m in metrics):
        result.finished_at = utcnow()
        result.detail = "no terminal payment outcomes inside the window"
        return result

    # Opt-in same-time-yesterday baseline (default: leading window — the
    # published operating point; docs/detection.md "Known limitations"). The
    # baseline records come from the SAME clock window shifted back 24h; the
    # series handed to the detectors is [yesterday's window] + [today's
    # window], with the detector baseline sized to yesterday's decidable
    # buckets so only today's buckets are ever scored.
    sty = req.baseline_mode == "same_time_yesterday"
    baseline_start = window_start - SAME_TIME_YESTERDAY_SHIFT
    baseline_end = window_end - SAME_TIME_YESTERDAY_SHIFT
    baseline_outcomes = (
        load_outcomes(db, baseline_start, baseline_end, req.segment, source_types)
        if sty
        else None
    )

    for metric in metrics:
        bucket_minutes = req.bucket_minutes * METRIC_BUCKET_MULTIPLIER.get(metric, 1)
        if metric in ATTEMPT_BASED_METRICS:
            records: list = load_checkout_attempts(
                db,
                window_start,
                window_end,
                req.segment,
                inactivity_minutes=ABANDONMENT_INACTIVITY_MINUTES,
                source_types=source_types,
            )
            baseline_records = (
                load_checkout_attempts(
                    db,
                    baseline_start,
                    baseline_end,
                    req.segment,
                    inactivity_minutes=ABANDONMENT_INACTIVITY_MINUTES,
                    source_types=source_types,
                )
                if sty
                else None
            )
        else:
            records = outcomes
            baseline_records = baseline_outcomes
        today_series = build_metric_series(
            records,
            metric=metric,
            window_start=window_start,
            window_end=window_end,
            bucket_minutes=bucket_minutes,
        )
        series = today_series
        n_baseline_valid: int | None = None
        baseline_series: list[Bucket] | None = None
        min_count = _metric_floor(req, "min_bucket_count", METRIC_MIN_BUCKET_COUNT, metric)
        if sty:
            baseline_series = build_metric_series(
                baseline_records or [],
                metric=metric,
                window_start=baseline_start,
                window_end=baseline_end,
                bucket_minutes=bucket_minutes,
            )
            # Exactly yesterday's decidable buckets (the same validity rule
            # the detectors apply) form the baseline; only today's buckets
            # are scored.
            n_baseline_valid = sum(
                1 for b in baseline_series if b.value is not None and b.count >= min_count
            )
            series = [*baseline_series, *today_series]
            if n_baseline_valid < STY_MIN_BASELINE_BUCKETS:
                logger.info(
                    "same_time_yesterday baseline too sparse; metric skipped",
                    extra={
                        "run_id": run_id,
                        "metric": metric,
                        "valid_baseline_buckets": n_baseline_valid,
                    },
                )
                continue
        admitted_detectors: set[str] = set()
        for detector in detectors:
            params = DetectorParams(
                baseline_buckets=n_baseline_valid if sty else req.baseline_buckets,
                threshold=req.threshold,
                sensitivity=req.sensitivity,
                min_bucket_count=min_count,
                direction=METRIC_DIRECTION[metric],
                bucket_minutes=bucket_minutes,
            )
            anomaly = detector.detect(series, params)
            if anomaly is None:
                continue
            report = _admit(
                db,
                result,
                run_id=run_id,
                metric=metric,
                detector_name=detector.name,
                anomaly=anomaly,
                series=series,
                records=records,
                segment=req.segment or {},
                window_start=window_start,
                window_end=window_end,
                bucket_minutes=bucket_minutes,
                # Localization keeps its own leading-window slice baseline;
                # the request value — not yesterday's bucket count — sizes it.
                params=replace(params, baseline_buckets=req.baseline_buckets) if sty else params,
                req=req,
                now=started_at,
                environment=environment,
                extra_meta={"baseline_mode": req.baseline_mode} if sty else None,
            )
            if report is not None:
                admitted_detectors.add(detector.name)
        if (
            metric == METRIC_CAPTURE_LATENCY
            and LATENCY_ROUTE_SCAN_ENABLED
            and not req.segment
        ):
            # Blind-spot cover: the aggregate said nothing (or only fired
            # below the floors) — drill into per-route latency slices.
            _scan_latency_routes(
                db,
                result,
                run_id=run_id,
                outcomes=outcomes,
                baseline_outcomes=baseline_outcomes,
                detectors=detectors,
                skip_detectors=admitted_detectors,
                window_start=window_start,
                window_end=window_end,
                req=req,
                now=started_at,
                environment=environment,
            )

    if not req.dry_run:
        db.commit()

    result.finished_at = utcnow()
    result.detail = (
        f"window=[{window_start.isoformat()}..{window_end.isoformat()}), "
        f"metrics={metrics}, detectors={[d.name for d in detectors]}, "
        f"outcomes={len(outcomes)}, anomalies={result.anomalies_detected}"
        + (f", filtered={result.anomalies_filtered}" if result.anomalies_filtered else "")
        + (f", baseline_mode={req.baseline_mode}" if sty else "")
        + (", night_regime_floors=on" if req.night_regime_floors else "")
        + (" (dry_run: nothing persisted)" if req.dry_run else "")
    )
    logger.info(
        "detection_run",
        extra={
            "run_id": run_id,
            "anomalies": result.anomalies_detected,
            "anomalies_filtered": result.anomalies_filtered,
            "incidents_created": len(result.incidents_created),
            "incidents_updated": len(result.incidents_updated),
            "dry_run": req.dry_run,
        },
    )
    return result


# ---------------------------------------------------------------------------
# Incident-level noise floors
# ---------------------------------------------------------------------------


def _metric_floor(
    req: DetectionRunRequest, field: str, per_metric: dict[str, int], metric: str
) -> int:
    """Effective floor value: an explicit request field always wins; otherwise
    the metric's own default (when one exists) overrides the request default.
    This is how the small-volume signals get their own floors without new
    request surface."""
    if field in req.model_fields_set:
        return getattr(req, field)
    return per_metric.get(metric, getattr(req, field))


def _is_night_regime_anomaly(anomaly: Anomaly) -> bool:
    """True when every flagged bucket starts inside the night band (UTC hour
    >= NIGHT_REGIME_START_HOUR or < NIGHT_REGIME_END_HOUR). One daytime
    bucket in the episode disqualifies it — the lower bar must never judge a
    mix that organic daytime clusters can reach."""
    flagged = anomaly.flagged_ts or (anomaly.start_ts,)
    return all(
        ts.hour >= NIGHT_REGIME_START_HOUR or ts.hour < NIGHT_REGIME_END_HOUR
        for ts in flagged
    )


def _night_floors_apply(metric: str, anomaly: Anomaly, req: DetectionRunRequest) -> bool:
    """Whether the opt-in night-regime floor set — not the global floors —
    judges this anomaly: the request turned the mode on, the metric carries a
    night set, and every flagged bucket sits in the night band."""
    return (
        req.night_regime_floors
        and metric in NIGHT_MIN_OBSERVED
        and _is_night_regime_anomaly(anomaly)
    )


def _flagged_run_and_volume(
    anomaly: Anomaly, series: list[Bucket]
) -> tuple[int, int]:
    """(longest run of consecutive flagged buckets, total events in flagged
    buckets) — the persistence and affected-volume signals for the floors."""
    flagged = set(anomaly.flagged_ts)
    volume = 0
    longest = run = 0
    for b in series:
        if b.ts in flagged:
            volume += b.count
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest, volume


def _floor_violation(
    anomaly: Anomaly,
    series: list[Bucket],
    *,
    metric: str,
    req: DetectionRunRequest,
) -> str | None:
    """Return the reason a detector fire fails the incident-level noise
    floors, or None when it clears them. Floors are engine-side (detectors
    stay pure statistics); every floor is request-configurable, with
    per-metric defaults where a signal lives in the small-volume regime.
    With the opt-in ``night_regime_floors`` mode on, an all-night anomaly on
    a metric that carries a night floor set is judged by that (lower) set
    instead of the global share/absolute bars."""
    night = _night_floors_apply(metric, anomaly, req)
    floor = req.min_absolute_deviation
    if floor is None:
        floor = (
            NIGHT_MIN_ABSOLUTE_DEVIATION[metric]
            if night
            else DEFAULT_MIN_ABSOLUTE_DEVIATION[metric]
        )
    abs_dev = abs(anomaly.observed - anomaly.baseline)
    if abs_dev < floor:
        return f"|observed-baseline| {abs_dev:.4g} < min_absolute_deviation {floor:.4g}"
    min_observed = (NIGHT_MIN_OBSERVED if night else METRIC_MIN_OBSERVED).get(metric)
    if min_observed is not None and anomaly.observed < min_observed:
        return (
            f"observed {anomaly.observed:.4g} < min_observed {min_observed:.4g} "
            "(a wave must dominate the mix, not merely elevate it)"
        )
    min_volume = _metric_floor(req, "min_flagged_volume", METRIC_MIN_FLAGGED_VOLUME, metric)
    min_run = _metric_floor(req, "min_flagged_run", METRIC_MIN_FLAGGED_RUN, metric)
    longest_run, volume = _flagged_run_and_volume(anomaly, series)
    if volume < min_volume:
        return f"flagged volume {volume} < min_flagged_volume {min_volume}"
    if longest_run < min_run:
        return (
            f"persistence {longest_run} consecutive bucket(s) "
            f"< min_flagged_run {min_run}"
        )
    return None


def _admit(
    db: Session,
    result: DetectionRunResult,
    *,
    run_id: str,
    metric: str,
    detector_name: str,
    anomaly: Anomaly,
    series: list[Bucket],
    records: list,
    segment: dict[str, str],
    window_start: datetime,
    window_end: datetime,
    bucket_minutes: int,
    params: DetectorParams,
    req: DetectionRunRequest,
    now: datetime,
    environment: str,
    extra_meta: dict | None = None,
) -> IncidentReport | None:
    """The admission gate between a detector fire and an incident: floors,
    localization, impact, persistence. Returns the report when the fire
    cleared the floors (created/updated/would_*/suppressed), None when a
    floor rejected it."""
    floor_reason = _floor_violation(anomaly, series, metric=metric, req=req)
    if floor_reason is not None:
        result.anomalies_filtered += 1
        logger.info(
            "detection_floor_filtered",
            extra={
                "run_id": run_id,
                "metric": metric,
                "detector": detector_name,
                "reason": floor_reason,
            },
        )
        return None
    localization = localize(
        records,
        metric=metric,
        anomaly=anomaly,
        window_start=window_start,
        window_end=window_end,
        params=params,
    )
    affected, revenue_at_risk = _impact(records, metric, anomaly, window_end)
    if _night_floors_apply(metric, anomaly, req):
        # honesty marker: this fire was admitted under the lower night floor
        # set, not the published global operating point
        extra_meta = {**(extra_meta or {}), "night_regime_floors": True}
    report = _persist(
        db,
        run_id=run_id,
        metric=metric,
        detector_name=detector_name,
        anomaly=anomaly,
        series=series,
        localization=localization,
        segment=segment,
        window_start=window_start,
        window_end=window_end,
        bucket_minutes=bucket_minutes,
        affected=affected,
        revenue_at_risk=revenue_at_risk,
        dry_run=req.dry_run,
        now=now,
        dedup_cooldown_minutes=req.dedup_cooldown_minutes,
        suppress_after_resolve_minutes=req.suppress_after_resolve_minutes,
        environment=environment,
        extra_meta=extra_meta,
    )
    result.incidents.append(report)
    if report.action == "suppressed":
        result.anomalies_filtered += 1
        return report
    result.anomalies_detected += 1
    if report.action == "created":
        result.incidents_created.append(report.incident_id or "")
    elif report.action == "updated":
        result.incidents_updated.append(report.incident_id or "")
    return report


def _slice_latency_corroborated(
    slice_records: list[PaymentOutcome],
    anomaly: Anomaly,
    *,
    bucket_minutes: int,
    min_samples: int = 3,
    min_ratio: float = 2.0,
    lone_ratio: float = 3.0,
) -> bool:
    """Mix-shift guard for route latency slices: a real route degradation
    slows every method flowing over the route; a method-mix shift only moves
    the aggregate mean. The rise must therefore hold WITHIN methods: at least
    two methods with >= ``min_samples`` successful captures in both the
    pre-anomaly and the anomaly region must each rise >= ``min_ratio``; when
    only one method qualifies, it must rise >= ``lone_ratio``. Measured on
    standard/seed42: the injected route latency rises >= 7x in every method
    on the route, while organic slice fires rise in at most one method (the
    rest stay flat — that is the mix shifting, not the route degrading)."""
    span_end = anomaly.end_ts + timedelta(minutes=bucket_minutes)
    base: dict[str, list[float]] = {}
    anom: dict[str, list[float]] = {}
    for o in slice_records:
        if not o.success or o.latency_ms is None:
            continue
        method = o.segments.get("method", UNKNOWN_SEGMENT)
        if o.ts < anomaly.start_ts:
            base.setdefault(method, []).append(o.latency_ms)
        elif o.ts < span_end:
            anom.setdefault(method, []).append(o.latency_ms)
    ratios: list[float] = []
    for method, b_vals in base.items():
        a_vals = anom.get(method)
        if a_vals is None or len(b_vals) < min_samples or len(a_vals) < min_samples:
            continue
        b_mean = sum(b_vals) / len(b_vals)
        if b_mean <= 0:
            continue
        ratios.append((sum(a_vals) / len(a_vals)) / b_mean)
    strong = [r for r in ratios if r >= min_ratio]
    if len(strong) >= 2:
        return True
    return len(ratios) == 1 and ratios[0] >= lone_ratio


def _scan_latency_routes(
    db: Session,
    result: DetectionRunResult,
    *,
    run_id: str,
    outcomes: list[PaymentOutcome],
    baseline_outcomes: list[PaymentOutcome] | None,
    detectors: list,
    skip_detectors: set[str],
    window_start: datetime,
    window_end: datetime,
    req: DetectionRunRequest,
    now: datetime,
    environment: str,
) -> None:
    """Per-route capture-latency scan — the route_latency blind-spot cover.

    Runs only when the merchant-wide latency pass admitted nothing for the
    detector (``skip_detectors``), so fleet-wide latency incidents never
    produce duplicate slice incidents. Each slice is scored with the same
    detector and the standard incident floors; slice series use coarser
    buckets and a lower bucket-count floor because one route carries a
    fraction of traffic (measured: 5-min route slices at night hold 1-4
    captures, 15-min slices 2-8 — see ml/experiments/detection/exp001).
    In ``same_time_yesterday`` mode each slice's baseline is yesterday's
    same-clock slice, exactly like the aggregate pass."""
    metric = METRIC_CAPTURE_LATENCY
    scan_bucket_minutes = req.bucket_minutes * LATENCY_SCAN_BUCKET_MULTIPLIER
    routes = sorted(
        {o.segments.get(LATENCY_SCAN_DIMENSION, UNKNOWN_SEGMENT) for o in outcomes}
        - {UNKNOWN_SEGMENT}
    )
    for route in routes:
        slice_records = [
            o
            for o in outcomes
            if o.segments.get(LATENCY_SCAN_DIMENSION, UNKNOWN_SEGMENT) == route
        ]
        if len(slice_records) >= LATENCY_SCAN_MAX_SLICE_SHARE * len(outcomes):
            continue  # a slice that IS the aggregate localizes nothing
        series = build_series(
            slice_records,
            metric=metric,
            window_start=window_start,
            window_end=window_end,
            bucket_minutes=scan_bucket_minutes,
        )
        baseline_buckets = req.baseline_buckets
        if baseline_outcomes is not None:
            slice_baseline = [
                o
                for o in baseline_outcomes
                if o.segments.get(LATENCY_SCAN_DIMENSION, UNKNOWN_SEGMENT) == route
            ]
            baseline_series = build_series(
                slice_baseline,
                metric=metric,
                window_start=window_start - SAME_TIME_YESTERDAY_SHIFT,
                window_end=window_end - SAME_TIME_YESTERDAY_SHIFT,
                bucket_minutes=scan_bucket_minutes,
            )
            n_valid = sum(
                1
                for b in baseline_series
                if b.value is not None and b.count >= LATENCY_SCAN_MIN_BUCKET_COUNT
            )
            if n_valid < STY_MIN_BASELINE_BUCKETS:
                continue  # no honest yesterday baseline for this slice
            series = [*baseline_series, *series]
            baseline_buckets = n_valid
        for detector in detectors:
            if detector.name in skip_detectors:
                continue
            params = DetectorParams(
                baseline_buckets=baseline_buckets,
                threshold=req.threshold,
                sensitivity=req.sensitivity,
                min_bucket_count=LATENCY_SCAN_MIN_BUCKET_COUNT,
                direction=METRIC_DIRECTION[metric],
                bucket_minutes=scan_bucket_minutes,
            )
            anomaly = detector.detect(series, params)
            if anomaly is None:
                continue
            if not _slice_latency_corroborated(
                slice_records, anomaly, bucket_minutes=scan_bucket_minutes
            ):
                result.anomalies_filtered += 1
                logger.info(
                    "detection_floor_filtered",
                    extra={
                        "run_id": run_id,
                        "metric": metric,
                        "detector": detector.name,
                        "reason": "slice latency rise not corroborated within "
                        "methods (mix shift, not route degradation)",
                    },
                )
                continue
            _admit(
                db,
                result,
                run_id=run_id,
                metric=metric,
                detector_name=detector.name,
                anomaly=anomaly,
                series=series,
                records=slice_records,
                segment={LATENCY_SCAN_DIMENSION: route},
                window_start=window_start,
                window_end=window_end,
                bucket_minutes=scan_bucket_minutes,
                # Localization keeps its own leading-window slice baseline
                # (see the aggregate pass); only the detector gets
                # yesterday's bucket count.
                params=(
                    replace(params, baseline_buckets=req.baseline_buckets)
                    if baseline_outcomes is not None
                    else params
                ),
                req=req,
                now=now,
                environment=environment,
                extra_meta=(
                    {"segment_scan": True, "baseline_mode": req.baseline_mode}
                    if baseline_outcomes is not None
                    else {"segment_scan": True}
                ),
            )


# ---------------------------------------------------------------------------
# Localization
# ---------------------------------------------------------------------------


def localize(
    records: list,
    *,
    metric: str,
    anomaly: Anomaly,
    window_start: datetime,
    window_end: datetime,
    params: DetectorParams,
) -> dict[str, list[dict]]:
    """Rank per-dimension segment values by how much they deviated inside the
    anomalous region. ``flagged`` means the slice deviates in the degradation
    direction by at least half the global deviation. Works on both
    outcome-based and attempt-based records (series dispatch by metric)."""
    direction = params.direction
    breakdown: dict[str, list[dict]] = {}
    for dimension in SEGMENT_DIMENSIONS:
        entries: list[dict] = []
        for value, group in slice_outcomes(records, dimension).items():
            if len(group) < params.min_bucket_count:
                continue
            series = build_metric_series(
                group,
                metric=metric,
                window_start=window_start,
                window_end=window_end,
                bucket_minutes=params.bucket_minutes,
            )
            valid = [b for b in series if b.value is not None]
            baseline_vals = [b.value for b in valid[: params.baseline_buckets]]
            region_vals = [b.value for b in valid if b.ts >= anomaly.start_ts]
            if not baseline_vals or not region_vals:
                continue
            baseline = sum(baseline_vals) / len(baseline_vals)
            observed = min(region_vals) if direction == "down" else max(region_vals)
            deviation = (
                (observed - baseline) / abs(baseline) * 100.0 if baseline else 0.0
            )
            direction_matches = deviation < 0 if direction == "down" else deviation > 0
            entries.append(
                {
                    "value": value,
                    "events": len(group),
                    "baseline": round(baseline, 6),
                    "observed": round(observed, 6),
                    "deviation_pct": round(deviation, 2),
                    "flagged": bool(
                        direction_matches
                        and abs(deviation) >= 0.5 * abs(anomaly.deviation_pct)
                    ),
                }
            )
        entries.sort(key=lambda e: abs(e["deviation_pct"]), reverse=True)
        breakdown[dimension] = entries[:TOP_SEGMENTS_PER_DIMENSION]
    return breakdown


def _impact(
    records: list,
    metric: str,
    anomaly: Anomaly,
    window_end: datetime,
) -> tuple[int, int]:
    """Preliminary revenue-at-risk: the payments the incident plausibly put
    at risk, from the estimated degradation start to the window end —
    failed (success rate), abnormally slow (latency), abandoned checkouts
    (abandonment rate), or insufficient-funds failures (error share)."""
    region = [o for o in records if anomaly.start_ts <= o.ts < window_end]
    if metric == METRIC_CAPTURE_LATENCY:
        affected = [o for o in region if o.success and o.latency_ms is not None and o.latency_ms > anomaly.baseline]
    elif metric == METRIC_CHECKOUT_ABANDONMENT:
        affected = [o for o in region if o.abandoned]
    elif metric == METRIC_INSUFFICIENT_FUND_SHARE:
        affected = [o for o in region if not o.success and is_insufficient_fund(o.error_reason)]
    else:
        affected = [o for o in region if not o.success]
    return len(affected), sum(o.amount_paise for o in affected)


# ---------------------------------------------------------------------------
# Persistence (idempotent upsert + cross-pass episode merge + suppression)
# ---------------------------------------------------------------------------


def _segment_fingerprint(segment: dict[str, str]) -> str:
    canonical = json.dumps(segment or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode()).hexdigest()[:16]


def _parse_ts(value: object) -> datetime:
    ts = datetime.fromisoformat(str(value))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _episode_span(incident: Incident) -> tuple[datetime | None, datetime | None]:
    """The incident's estimated episode bounds: meta.anomaly_start/end when
    present, else the analysis window. Used for cross-window episode matching."""
    meta = incident.meta or {}
    try:
        return _parse_ts(meta["anomaly_start"]), _parse_ts(meta["anomaly_end"])
    except (KeyError, ValueError):
        return incident.window_start, incident.window_end


def _find_match(
    candidates: list[Incident],
    *,
    anomaly: Anomaly,
    window_start: datetime,
    window_end: datetime,
    dedup_cooldown_minutes: int | None,
) -> tuple[Incident | None, bool]:
    """Find the incident this anomaly belongs to: exact same-window upsert
    first (idempotent re-run), then cross-window episode merge — an OPEN
    (non-terminal) incident with the same signature whose anomaly span
    overlaps the new one or lies within the cooldown gap. Returns
    (incident, merged) where ``merged`` marks a cross-window episode merge."""
    exact = next(
        (
            i
            for i in candidates
            if i.window_start == window_start and i.window_end == window_end
        ),
        None,
    )
    if exact is not None:
        return exact, False
    if dedup_cooldown_minutes is None:
        return None, False
    gap = timedelta(minutes=dedup_cooldown_minutes)
    overlapping = []
    for inc in candidates:
        if inc.status in TERMINAL_STATUSES:
            continue
        start, end = _episode_span(inc)
        if start is None or end is None:
            continue
        if anomaly.start_ts <= end + gap and start <= anomaly.end_ts + gap:
            overlapping.append(inc)
    if not overlapping:
        return None, False
    # the original detection owns the episode (earliest detected_at) — that
    # keeps detected_at / window bounds / MTTD honest across merges
    return min(overlapping, key=lambda i: i.detected_at), True


def _find_suppressor(
    candidates: list[Incident],
    *,
    anomaly: Anomaly,
    suppress_after_resolve_minutes: int | None,
) -> Incident | None:
    """Post-resolution suppression: a terminal (RESOLVED/CLOSED/
    FALSE_POSITIVE) incident with the same signature, resolved recently enough
    that the new anomaly start falls inside the suppression window — the
    episode tail must not reopen what a human already closed."""
    if suppress_after_resolve_minutes is None:
        return None
    gap = timedelta(minutes=suppress_after_resolve_minutes)
    terminal = [i for i in candidates if i.status in TERMINAL_STATUSES]
    # most recent resolution first
    terminal.sort(
        key=lambda i: i.resolved_at or i.updated_at or i.detected_at, reverse=True
    )
    for inc in terminal:
        ref = inc.resolved_at or inc.updated_at or inc.detected_at
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        if anomaly.start_ts <= ref + gap:
            return inc
    return None


def _persist(
    db: Session,
    *,
    run_id: str,
    metric: str,
    detector_name: str,
    anomaly: Anomaly,
    series: list[Bucket],
    localization: dict[str, list[dict]],
    segment: dict[str, str],
    window_start: datetime,
    window_end: datetime,
    bucket_minutes: int,
    affected: int,
    revenue_at_risk: int,
    dry_run: bool,
    now: datetime,
    dedup_cooldown_minutes: int | None,
    suppress_after_resolve_minutes: int | None,
    environment: str,
    extra_meta: dict | None = None,
) -> IncidentReport:
    fingerprint = _segment_fingerprint(segment)
    # Dedup/merge/suppression candidates are environment-scoped: a real_test
    # pass must never merge into (or be suppressed by) a research incident
    # with the same signature, and vice versa.
    candidates = [
        i
        for i in db.scalars(
            sa.select(Incident).where(
                Incident.metric == metric,
                Incident.detection_method == detector_name,
                Incident.environment == environment,
            )
        ).all()
        if (i.meta or {}).get("segment_fingerprint") == fingerprint
    ]
    match, merged = _find_match(
        candidates,
        anomaly=anomaly,
        window_start=window_start,
        window_end=window_end,
        dedup_cooldown_minutes=dedup_cooldown_minutes,
    )
    suppressor = None
    if match is None:
        suppressor = _find_suppressor(
            candidates,
            anomaly=anomaly,
            suppress_after_resolve_minutes=suppress_after_resolve_minutes,
        )

    severity = severity_for_deviation(anomaly.deviation_pct)
    if suppressor is not None:
        return IncidentReport(
            incident_id=suppressor.id,
            action="suppressed",
            metric=metric,
            detector=detector_name,
            severity=severity,
            baseline_value=round(anomaly.baseline, 6),
            observed_value=round(anomaly.observed, 6),
            deviation_pct=round(anomaly.deviation_pct, 2),
            segment=segment,
            window_start=window_start,
            window_end=window_end,
            detected_at=None,
            anomaly_start=anomaly.start_ts,
            affected_payments_count=affected,
            revenue_at_risk_paise=revenue_at_risk,
            detail=(
                f"re-detection of {suppressor.status.value} incident "
                f"{suppressor.id} suppressed "
                f"(suppress_after_resolve_minutes={suppress_after_resolve_minutes})"
            ),
        )

    if match is None:
        action = "would_create" if dry_run else "created"
    else:
        action = "would_update" if dry_run else "updated"
    detail = (
        f"merged into open episode incident {match.id} "
        f"(dedup_cooldown_minutes={dedup_cooldown_minutes})"
        if merged and match is not None
        else None
    )

    if dry_run:
        return IncidentReport(
            incident_id=match.id if match else None,
            action=action,
            metric=metric,
            detector=detector_name,
            severity=severity,
            baseline_value=round(anomaly.baseline, 6),
            observed_value=round(anomaly.observed, 6),
            deviation_pct=round(anomaly.deviation_pct, 2),
            segment=segment,
            window_start=window_start,
            window_end=window_end,
            detected_at=match.detected_at if match else None,
            anomaly_start=anomaly.start_ts,
            affected_payments_count=affected,
            revenue_at_risk_paise=revenue_at_risk,
            detail=detail,
        )

    meta = {
        "segment": segment,
        "segment_fingerprint": fingerprint,
        "detector": {"name": detector_name},
        "bucket_minutes": bucket_minutes,
        "anomaly_start": anomaly.start_ts.isoformat(),
        "anomaly_end": anomaly.end_ts.isoformat(),
        "score": round(anomaly.score, 4),
        "flagged_buckets": [t.isoformat() for t in anomaly.flagged_ts],
        "run_id": run_id,
        "last_confirmed_at": now.isoformat(),
    }
    if extra_meta:
        meta.update(extra_meta)
    if merged and match is not None:
        # widen the episode span, never narrow it: the earliest estimated
        # start and the latest evidence end describe the whole episode
        prev_start, prev_end = _episode_span(match)
        meta["anomaly_start"] = (
            min(prev_start, anomaly.start_ts) if prev_start else anomaly.start_ts
        ).isoformat()
        meta["anomaly_end"] = (
            max(prev_end, anomaly.end_ts) if prev_end else anomaly.end_ts
        ).isoformat()
        meta["merge_count"] = int((match.meta or {}).get("merge_count", 0)) + 1
    evidence_rows = _build_evidence(
        incident_id=match.id if match else None,
        metric=metric,
        series=series,
        localization=localization,
        now=now,
        environment=environment,
    )

    if match is None:
        incident = Incident(
            title=_title(metric, anomaly, segment),
            description=_description(detector_name, metric, anomaly, window_start, window_end),
            status=IncidentStatus.OPEN,
            severity=severity,
            metric=metric,
            detection_method=detector_name,
            environment=environment,
            baseline_value=round(anomaly.baseline, 6),
            observed_value=round(anomaly.observed, 6),
            deviation_pct=round(anomaly.deviation_pct, 2),
            window_start=window_start,
            window_end=window_end,
            detected_at=now,
            affected_payments_count=affected,
            revenue_at_risk_paise=revenue_at_risk,
            currency="INR",
            meta=meta,
        )
        db.add(incident)
        db.flush()  # assign id before evidence rows reference it
        for row in evidence_rows:
            row.incident_id = incident.id
            db.add(row)
        detected_at = incident.detected_at
        incident_id = incident.id
    else:
        incident = match
        # Status is deliberately left untouched (a human may have triaged it);
        # original detected_at and window bounds are preserved so MTTD stays
        # honest across both same-window re-runs and cross-window merges.
        incident.severity = severity
        incident.baseline_value = round(anomaly.baseline, 6)
        incident.observed_value = round(anomaly.observed, 6)
        incident.deviation_pct = round(anomaly.deviation_pct, 2)
        incident.affected_payments_count = affected
        incident.revenue_at_risk_paise = revenue_at_risk
        incident.meta = {**(incident.meta or {}), **meta}
        for row in list(incident.evidence):
            if row.collector == COLLECTOR:
                db.delete(row)
        for row in evidence_rows:
            row.incident_id = incident.id
            db.add(row)
        detected_at = incident.detected_at
        incident_id = incident.id

    return IncidentReport(
        incident_id=incident_id,
        action=action,
        metric=metric,
        detector=detector_name,
        severity=severity,
        baseline_value=round(anomaly.baseline, 6),
        observed_value=round(anomaly.observed, 6),
        deviation_pct=round(anomaly.deviation_pct, 2),
        segment=segment,
        window_start=window_start,
        window_end=window_end,
        detected_at=detected_at,
        anomaly_start=anomaly.start_ts,
        affected_payments_count=affected,
        revenue_at_risk_paise=revenue_at_risk,
        detail=detail,
    )


def _build_evidence(
    *,
    incident_id: str | None,
    metric: str,
    series: list[Bucket],
    localization: dict[str, list[dict]],
    now: datetime,
    environment: str,
) -> list[IncidentEvidence]:
    snapshot = IncidentEvidence(
        incident_id=incident_id or "",
        evidence_type="metric_series",
        title=f"{metric} bucketed series snapshot",
        environment=environment,
        payload={
            "metric": metric,
            "buckets": [
                {
                    "ts": b.ts.isoformat(),
                    "value": b.value,
                    "count": b.count,
                }
                for b in series
            ],
        },
        collector=COLLECTOR,
        collected_at=now,
    )
    segments = IncidentEvidence(
        incident_id=incident_id or "",
        evidence_type="segment_breakdown",
        title="Top contributing segments",
        environment=environment,
        payload={"dimensions": localization},
        collector=COLLECTOR,
        collected_at=now,
    )
    return [snapshot, segments]


def _fmt(x: float) -> str:
    return f"{x:.4g}"


def _title(metric: str, anomaly: Anomaly, segment: dict[str, str]) -> str:
    verb = "dropped" if anomaly.deviation_pct < 0 else "rose"
    seg = ""
    if segment:
        seg = " [" + ", ".join(f"{k}={v}" for k, v in sorted(segment.items())) + "]"
    return (
        f"{metric} {verb} {abs(anomaly.deviation_pct):.1f}% "
        f"(baseline {_fmt(anomaly.baseline)} -> observed {_fmt(anomaly.observed)}){seg}"
    )


def _description(
    detector: str,
    metric: str,
    anomaly: Anomaly,
    window_start: datetime,
    window_end: datetime,
) -> str:
    return (
        f"Detector '{detector}' flagged {metric} between {anomaly.start_ts.isoformat()} "
        f"and {anomaly.end_ts.isoformat()} within window "
        f"[{window_start.isoformat()}..{window_end.isoformat()}). "
        f"Baseline {_fmt(anomaly.baseline)}, worst observed {_fmt(anomaly.observed)} "
        f"({anomaly.deviation_pct:+.1f}%), score {anomaly.score:.2f}."
    )


__all__ = [
    "DetectionRunResult",
    "IncidentReport",
    "run_detection",
    "severity_for_deviation",
    "localize",
]

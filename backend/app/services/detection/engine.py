"""Detection engine: one detection pass over the payment_events stream.

A pass (``run_detection``) does:

1. Resolve the analysis window — anchored at the latest terminal event (or an
   explicit ``as_of``) so identical data yields an identical window.
2. Build the bucketed series per metric and run the chosen detector(s).
3. Apply the incident-level noise floors (Watchdog-style): a detector fire
   becomes an incident only when the deviation is big in absolute terms
   (``min_absolute_deviation``), touches enough traffic
   (``min_flagged_volume``), and persists across enough consecutive buckets
   (``min_flagged_run``). Fires that fail a floor are counted
   (``anomalies_filtered``) and dropped — organic night-traffic wobble is not
   an incident.
4. Localize each surviving anomaly by re-scoring per-segment slices
   (method / bank / gateway) and ranking contributors by deviation.
5. Persist: one ``incidents`` row per (metric, detector, window, segment) —
   re-running the same combination UPDATEs that row (original ``detected_at``
   preserved, evidence refreshed) instead of duplicating it. Cross-window
   re-detection of the SAME episode (overlapping scheduled passes) is merged
   into the open incident when the anomaly spans overlap or lie within
   ``dedup_cooldown_minutes``; re-detection of a signature that was resolved
   (RESOLVED/CLOSED/FALSE_POSITIVE) within ``suppress_after_resolve_minutes``
   is suppressed, not reopened.
6. Attach ``incident_evidence``: a ``metric_series`` snapshot and the
   ``segment_breakdown`` ranking.

Detection latency is computable from the persisted record: the true start
comes from simulator ground truth, and ``meta.anomaly_start`` /
``incidents.detected_at`` give the estimated start and detection time.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import ids
from app.db import utcnow
from app.logging import get_logger
from app.models import Incident, IncidentEvidence
from app.ports import IncidentStatus, Severity
from app.schemas.detection import DetectionRunRequest
from app.services.detection.detectors import (
    Anomaly,
    DetectorParams,
    all_detectors,
    get_detector,
)
from app.services.detection.series import (
    SEGMENT_DIMENSIONS,
    KNOWN_METRICS,
    METRIC_CAPTURE_LATENCY,
    METRIC_DIRECTION,
    METRIC_SUCCESS_RATE,
    Bucket,
    PaymentOutcome,
    build_series,
    floor_bucket,
    latest_event_anchor,
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
DEFAULT_MIN_ABSOLUTE_DEVIATION: dict[str, float] = {
    METRIC_SUCCESS_RATE: 0.05,
    METRIC_CAPTURE_LATENCY: 75.0,
}

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

    result = DetectionRunResult(run_id=run_id, status="completed", started_at=started_at)

    anchor = req.as_of or latest_event_anchor(db)
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

    outcomes = load_outcomes(db, window_start, window_end, req.segment)
    if not outcomes:
        result.finished_at = utcnow()
        result.detail = "no terminal payment outcomes inside the window"
        return result

    for metric in metrics:
        series = build_series(
            outcomes,
            metric=metric,
            window_start=window_start,
            window_end=window_end,
            bucket_minutes=req.bucket_minutes,
        )
        for detector in detectors:
            params = DetectorParams(
                baseline_buckets=req.baseline_buckets,
                threshold=req.threshold,
                sensitivity=req.sensitivity,
                min_bucket_count=req.min_bucket_count,
                direction=METRIC_DIRECTION[metric],
                bucket_minutes=req.bucket_minutes,
            )
            anomaly = detector.detect(series, params)
            if anomaly is None:
                continue
            floor_reason = _floor_violation(anomaly, series, metric=metric, req=req)
            if floor_reason is not None:
                result.anomalies_filtered += 1
                logger.info(
                    "detection_floor_filtered",
                    extra={
                        "run_id": run_id,
                        "metric": metric,
                        "detector": detector.name,
                        "reason": floor_reason,
                    },
                )
                continue
            localization = localize(
                outcomes,
                metric=metric,
                anomaly=anomaly,
                window_start=window_start,
                window_end=window_end,
                params=params,
            )
            affected, revenue_at_risk = _impact(
                outcomes, metric, anomaly, window_end
            )
            report = _persist(
                db,
                run_id=run_id,
                metric=metric,
                detector_name=detector.name,
                anomaly=anomaly,
                series=series,
                localization=localization,
                segment=req.segment or {},
                window_start=window_start,
                window_end=window_end,
                bucket_minutes=req.bucket_minutes,
                affected=affected,
                revenue_at_risk=revenue_at_risk,
                dry_run=req.dry_run,
                now=started_at,
                dedup_cooldown_minutes=req.dedup_cooldown_minutes,
                suppress_after_resolve_minutes=req.suppress_after_resolve_minutes,
            )
            result.incidents.append(report)
            if report.action == "suppressed":
                result.anomalies_filtered += 1
                continue
            result.anomalies_detected += 1
            if report.action == "created":
                result.incidents_created.append(report.incident_id or "")
            elif report.action == "updated":
                result.incidents_updated.append(report.incident_id or "")

    if not req.dry_run:
        db.commit()

    result.finished_at = utcnow()
    result.detail = (
        f"window=[{window_start.isoformat()}..{window_end.isoformat()}), "
        f"metrics={metrics}, detectors={[d.name for d in detectors]}, "
        f"outcomes={len(outcomes)}, anomalies={result.anomalies_detected}"
        + (f", filtered={result.anomalies_filtered}" if result.anomalies_filtered else "")
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
    stay pure statistics); every floor is request-configurable."""
    floor = req.min_absolute_deviation
    if floor is None:
        floor = DEFAULT_MIN_ABSOLUTE_DEVIATION[metric]
    abs_dev = abs(anomaly.observed - anomaly.baseline)
    if abs_dev < floor:
        return f"|observed-baseline| {abs_dev:.4g} < min_absolute_deviation {floor:.4g}"
    longest_run, volume = _flagged_run_and_volume(anomaly, series)
    if volume < req.min_flagged_volume:
        return f"flagged volume {volume} < min_flagged_volume {req.min_flagged_volume}"
    if longest_run < req.min_flagged_run:
        return (
            f"persistence {longest_run} consecutive bucket(s) "
            f"< min_flagged_run {req.min_flagged_run}"
        )
    return None


# ---------------------------------------------------------------------------
# Localization
# ---------------------------------------------------------------------------


def localize(
    outcomes: list[PaymentOutcome],
    *,
    metric: str,
    anomaly: Anomaly,
    window_start: datetime,
    window_end: datetime,
    params: DetectorParams,
) -> dict[str, list[dict]]:
    """Rank per-dimension segment values by how much they deviated inside the
    anomalous region. ``flagged`` means the slice deviates in the degradation
    direction by at least half the global deviation."""
    direction = params.direction
    breakdown: dict[str, list[dict]] = {}
    for dimension in SEGMENT_DIMENSIONS:
        entries: list[dict] = []
        for value, group in slice_outcomes(outcomes, dimension).items():
            if len(group) < params.min_bucket_count:
                continue
            series = build_series(
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
    outcomes: list[PaymentOutcome],
    metric: str,
    anomaly: Anomaly,
    window_end: datetime,
) -> tuple[int, int]:
    """Preliminary revenue-at-risk: failed (or abnormally slow) payments from
    the estimated degradation start to the window end."""
    region = [o for o in outcomes if anomaly.start_ts <= o.ts < window_end]
    if metric == METRIC_CAPTURE_LATENCY:
        affected = [o for o in region if o.success and o.latency_ms is not None and o.latency_ms > anomaly.baseline]
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
) -> IncidentReport:
    fingerprint = _segment_fingerprint(segment)
    candidates = [
        i
        for i in db.scalars(
            sa.select(Incident).where(
                Incident.metric == metric,
                Incident.detection_method == detector_name,
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
    )

    if match is None:
        incident = Incident(
            title=_title(metric, anomaly, segment),
            description=_description(detector_name, metric, anomaly, window_start, window_end),
            status=IncidentStatus.OPEN,
            severity=severity,
            metric=metric,
            detection_method=detector_name,
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
) -> list[IncidentEvidence]:
    snapshot = IncidentEvidence(
        incident_id=incident_id or "",
        evidence_type="metric_series",
        title=f"{metric} bucketed series snapshot",
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

"""Detection schemas — on-demand anomaly detection runs."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.ports import Severity


class DetectionRunRequest(BaseModel):
    window_minutes: int = Field(default=60, ge=5, le=24 * 60)
    metrics: list[str] | None = None  # default: all known metrics
    dry_run: bool = False  # detect but do not create incidents
    # --- additive detection controls (all optional, backwards compatible) ---
    detector: str = "zscore"  # registry name, or "all" to run every detector
    segment: dict[str, str] | None = None  # restrict the pass to a slice, e.g. {"method": "upi"}
    bucket_minutes: int = Field(default=5, ge=1, le=120)
    baseline_buckets: int = Field(default=8, ge=4, le=200)
    min_bucket_count: int = Field(default=5, ge=1, le=1000)
    sensitivity: float = Field(default=1.0, gt=0, le=5)  # >1 fires earlier, <1 stricter
    threshold: float | None = None  # explicit detector threshold override
    as_of: datetime | None = None  # window anchor; defaults to latest terminal event
    # --- incident-level noise floors (engine-side, post-detector; docs/detection.md) ---
    # A detector fire only becomes an incident when it clears all three floors:
    min_absolute_deviation: float | None = Field(default=None, ge=0)
    # absolute |observed - baseline| in metric units; None = per-metric default
    # (success rate 0.05 = 5pp, latency 75ms); 0 disables this floor
    min_flagged_volume: int = Field(default=15, ge=0, le=1_000_000)
    # min total events across flagged buckets (affected volume); 0 disables
    min_flagged_run: int = Field(default=2, ge=1, le=100)
    # min CONSECUTIVE flagged buckets (persistence: a 1-bucket blip is not an
    # incident); 1 disables. Note: isolation_forest flags boundary buckets by
    # design — pair it with min_flagged_run=1.
    # --- cross-pass episode dedup + post-resolution suppression ---
    dedup_cooldown_minutes: int | None = Field(default=360, ge=0, le=7 * 24 * 60)
    # an OPEN (non-terminal) incident with the same (metric, detection_method,
    # segment) whose anomaly span overlaps or lies within this gap is MERGED
    # (refreshed, original detected_at preserved), not duplicated; None disables
    suppress_after_resolve_minutes: int | None = Field(default=720, ge=0, le=30 * 24 * 60)
    # after an incident is RESOLVED/CLOSED/FALSE_POSITIVE, re-detection of the
    # same signature starting within this window is suppressed (reported as
    # action "suppressed", nothing persisted); None disables


class DetectionIncidentView(BaseModel):
    """One incident a detection run created or updated (or would, on dry_run)."""

    incident_id: str | None = None
    action: str  # created | updated | would_create | would_update | suppressed
    metric: str
    detector: str
    severity: Severity
    baseline_value: float | None = None
    observed_value: float | None = None
    deviation_pct: float | None = None
    segment: dict[str, str] = Field(default_factory=dict)
    window_start: datetime | None = None
    window_end: datetime | None = None
    detected_at: datetime | None = None
    anomaly_start: datetime | None = None
    affected_payments_count: int = 0
    revenue_at_risk_paise: int = 0
    currency: str = "INR"
    detail: str | None = None  # e.g. why an anomaly was suppressed


class DetectionRunResponse(BaseModel):
    run_id: str
    status: str  # completed | failed
    started_at: datetime
    finished_at: datetime | None = None
    anomalies_detected: int = 0
    incidents_created: list[str] = Field(default_factory=list)
    detail: str | None = None
    # --- additive (all optional, backwards compatible) ---
    incidents_updated: list[str] = Field(default_factory=list)
    incidents: list[DetectionIncidentView] = Field(default_factory=list)
    anomalies_filtered: int = 0  # detector fires dropped by floors/suppression

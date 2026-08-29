"""Dashboard endpoints — real aggregates over payment_events, incidents, the
revenue engine, and recovery actions. Empty database -> zeros + empty series;
no number on this surface is ever fabricated.

Every aggregate is scoped to ONE environment (query param `environment`,
default 'real_test'): commerce reads filter payments by the environment's
source_type set, derived reads filter on the environment column. A research
row can never leak into a real_test aggregate, or vice versa.

Definitions (documented in docs/evaluation.md and the OpenAPI descriptions):
- current success window: the hour ending at the latest terminal payment event
  (data-anchored, so a seeded demo DB shows real numbers regardless of wall
  clock); baseline: the 24h immediately before it.
- revenue_at_risk: sum of RevenueService.revenue_at_risk observed-loss point
  estimates over open incidents; each call persists the fresh point onto
  incidents.revenue_at_risk_paise (audited when the value changes).
- recovered: recovery_actions in RECOVERED (webhook/inline verified) only.
- lost: max(0, observed_loss - actual_recovered) over terminal incidents
  (RESOLVED/CLOSED) — revenue the incident window lost for good.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.models import Incident, RecoveryAction, source_types_for_environment
from app.ports import IncidentStatus, RecoveryStatus
from app.schemas.common import TimeSeriesPoint
from app.schemas.dashboard import DashboardSummary, DashboardTimeseries
from app.schemas.incidents import IncidentSummary
from app.services.detection.series import (
    KNOWN_METRICS,
    PaymentOutcome,
    build_series,
    floor_bucket,
    latest_event_anchor,
    load_outcomes,
)
from app.services.policy import audit
from app.services.revenue import RevenueService
from app.services.revenue.types import RevenueAtRiskReport

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

OPEN_INCIDENT_STATUSES = (
    IncidentStatus.OPEN,
    IncidentStatus.INVESTIGATING,
    IncidentStatus.DIAGNOSED,
    IncidentStatus.RECOVERING,
)
TERMINAL_INCIDENT_STATUSES = (IncidentStatus.RESOLVED, IncidentStatus.CLOSED)
# Actions with money potentially in flight.
ACTIVE_ACTION_STATUSES = (
    RecoveryStatus.PENDING_APPROVAL,
    RecoveryStatus.APPROVED,
    RecoveryStatus.EXECUTING,
    RecoveryStatus.VERIFYING,
)
_RECENT_INCIDENTS = 5
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_BUCKET_MINUTES = {"minute": 1, "hour": 60, "day": 24 * 60}
_EXTRA_METRICS = (
    "payments_total",
    "payments_failed",
    "failed_amount_paise",
    "recovered_revenue_paise",
)


def _rate(outcomes: list[PaymentOutcome]) -> tuple[float, int]:
    n = len(outcomes)
    if not n:
        return 0.0, 0
    ok = sum(1 for o in outcomes if o.success)
    return ok / n, n


def _incident_summary(inc: Incident) -> IncidentSummary:
    return IncidentSummary(
        id=inc.id,
        title=inc.title,
        status=inc.status,
        severity=inc.severity,
        metric=inc.metric,
        detection_method=inc.detection_method,
        detected_at=inc.detected_at,
        baseline_value=inc.baseline_value,
        observed_value=inc.observed_value,
        deviation_pct=inc.deviation_pct,
        affected_payments_count=inc.affected_payments_count,
        revenue_at_risk_paise=inc.revenue_at_risk_paise,
        currency=inc.currency or "INR",
        environment=inc.environment or "research",
    )


def refresh_revenue_at_risk(
    db: Session, report: RevenueAtRiskReport, incident: Incident, *, actor: str
) -> int | None:
    """Persist a freshly computed observed-loss point estimate onto
    incidents.revenue_at_risk_paise. Audited on change; a None point (no
    baseline signal) leaves the stored value untouched. Flushes but never
    commits — the caller owns the transaction."""
    point = report.observed_loss.point_paise
    if point is not None and point != incident.revenue_at_risk_paise:
        entry = audit.record(
            db,
            actor=actor,
            action="incident.revenue_at_risk_refreshed",
            entity_type="incident",
            entity_id=incident.id,
            details={
                "from_paise": incident.revenue_at_risk_paise,
                "to_paise": point,
                "basis": report.observed_loss.basis,
            },
        )
        entry.environment = incident.environment or "research"
        incident.revenue_at_risk_paise = point
    return point


def _recovered_total(db: Session, environment: str) -> int:
    """Measured recovered revenue for one environment — the same semantics as
    RevenueService.recovered_revenue over the all-time window (RECOVERED
    actions only, coalesce(verified_at, completed_at) timestamp), scoped to
    the environment stamped on the actions."""
    ts_col = sa.func.coalesce(RecoveryAction.verified_at, RecoveryAction.completed_at)
    return int(
        db.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(RecoveryAction.amount_paise), 0)).where(
                RecoveryAction.status == RecoveryStatus.RECOVERED,
                RecoveryAction.environment == environment,
                ts_col >= _EPOCH,
                ts_col < utcnow() + timedelta(days=1),
            )
        )
        or 0
    )


@router.get("/summary", response_model=DashboardSummary)
def get_summary(
    db: Session = Depends(get_db),
    environment: Literal["real_test", "research"] = Query(default="real_test"),
) -> DashboardSummary:
    summary = DashboardSummary(environment=environment)
    source_types = source_types_for_environment(environment)

    # --- payment success rate: baseline vs current window ------------------
    anchor = latest_event_anchor(db, source_types=source_types)
    if anchor is not None:
        current_start = anchor - timedelta(hours=1)
        baseline_start = anchor - timedelta(hours=25)
        current, n_current = _rate(
            load_outcomes(db, current_start, anchor, source_types=source_types)
        )
        baseline, n_baseline = _rate(
            load_outcomes(db, baseline_start, current_start, source_types=source_types)
        )
        summary.payments_success_rate = round(current, 6)
        summary.payments_observed = n_current
        if n_baseline:
            summary.payments_baseline_success_rate = round(baseline, 6)

    # --- incidents -----------------------------------------------------------
    open_incidents = list(
        db.scalars(
            sa.select(Incident).where(
                Incident.status.in_(OPEN_INCIDENT_STATUSES),
                Incident.environment == environment,
            )
        )
    )
    summary.open_incidents = len(open_incidents)
    by_severity: dict[str, int] = {}
    for inc in open_incidents:
        by_severity[inc.severity.value] = by_severity.get(inc.severity.value, 0) + 1
    summary.incidents_by_severity = by_severity

    # --- revenue at risk / recoverable (counterfactual, per open incident) --
    revenue = RevenueService(db)
    at_risk = 0
    recoverable = 0
    low_confidence = False
    for inc in open_incidents:
        report = revenue.revenue_at_risk(inc.id)
        point = refresh_revenue_at_risk(db, report, inc, actor="system:dashboard")
        at_risk += point or 0
        recoverable += report.recoverable.point_paise or 0
        low_confidence = low_confidence or report.observed_loss.low_confidence
    summary.revenue_at_risk_paise = at_risk
    summary.recoverable_revenue_paise = recoverable
    summary.revenue_at_risk_low_confidence = low_confidence

    # --- recovered (measured, verified) + lost (terminal incidents) ---------
    summary.recovered_revenue_paise = _recovered_total(db, environment)

    lost = 0
    for inc in db.scalars(
        sa.select(Incident).where(
            Incident.status.in_(TERMINAL_INCIDENT_STATUSES),
            Incident.environment == environment,
        )
    ):
        report = revenue.revenue_at_risk(inc.id)
        lost += max(0, (report.observed_loss.point_paise or 0) - report.actual_recovered_paise)
    summary.lost_revenue_paise = lost

    denom = summary.recovered_revenue_paise + lost + at_risk
    summary.recovery_rate = (
        round(summary.recovered_revenue_paise / denom, 6) if denom else 0.0
    )

    # --- recovery pipeline ----------------------------------------------------
    summary.active_recoveries = int(
        db.scalar(
            sa.select(sa.func.count())
            .select_from(RecoveryAction)
            .where(
                RecoveryAction.status.in_(ACTIVE_ACTION_STATUSES),
                RecoveryAction.environment == environment,
            )
        )
        or 0
    )
    summary.pending_approvals = int(
        db.scalar(
            sa.select(sa.func.count())
            .select_from(RecoveryAction)
            .where(
                RecoveryAction.status == RecoveryStatus.PENDING_APPROVAL,
                RecoveryAction.environment == environment,
            )
        )
        or 0
    )

    summary.recent_incidents = [
        _incident_summary(inc)
        for inc in db.scalars(
            sa.select(Incident)
            .where(Incident.environment == environment)
            .order_by(Incident.detected_at.desc(), Incident.id.desc())
            .limit(_RECENT_INCIDENTS)
        )
    ]
    db.commit()  # persists the revenue_at_risk refreshes (+ audit rows)
    return summary


@router.get("/timeseries", response_model=DashboardTimeseries)
def get_timeseries(
    db: Session = Depends(get_db),
    metric: str = Query(default="payment_success_rate"),
    granularity: Literal["minute", "hour", "day"] = Query(default="hour"),
    window_hours: int = Query(default=24, ge=1, le=24 * 30),
    environment: Literal["real_test", "research"] = Query(default="real_test"),
) -> DashboardTimeseries:
    known = tuple(KNOWN_METRICS) + _EXTRA_METRICS
    if metric not in known:
        raise HTTPException(
            status_code=400,
            detail=f"unknown metric: {metric!r} (supported: {', '.join(known)})",
        )
    source_types = source_types_for_environment(environment)
    bucket_minutes = _BUCKET_MINUTES[granularity]
    anchor = latest_event_anchor(db, source_types=source_types) or utcnow()
    window_end = floor_bucket(anchor, bucket_minutes) + timedelta(minutes=bucket_minutes)
    window_start = window_end - timedelta(hours=window_hours)

    points: list[TimeSeriesPoint] = []
    if metric == "recovered_revenue_paise":
        ts_col = sa.func.coalesce(RecoveryAction.verified_at, RecoveryAction.completed_at)
        actions = db.scalars(
            sa.select(RecoveryAction).where(
                RecoveryAction.status == RecoveryStatus.RECOVERED,
                RecoveryAction.environment == environment,
                ts_col >= window_start,
                ts_col < window_end,
            )
        )
        buckets: dict[datetime, int] = {}
        for action in actions:
            ts = action.verified_at or action.completed_at
            if ts is None:
                continue
            key = floor_bucket(ts, bucket_minutes)
            buckets[key] = buckets.get(key, 0) + action.amount_paise
        points = [TimeSeriesPoint(ts=k, value=float(v)) for k, v in sorted(buckets.items())]
    else:
        outcomes = load_outcomes(db, window_start, window_end, source_types=source_types)
        if metric in KNOWN_METRICS:
            # Rates: buckets without observations carry no information -> skip.
            # Attempt/share-based detection metrics (e.g. checkout_abandonment_rate,
            # insufficient_fund_share) are detection-only for now: build_series
            # raises ValueError for them here -> answer 400, never a 500.
            try:
                series = build_series(
                    outcomes,
                    metric=metric,
                    window_start=window_start,
                    window_end=window_end,
                    bucket_minutes=bucket_minutes,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            points = [
                TimeSeriesPoint(ts=b.ts, value=round(b.value, 6))
                for b in series
                if b.value is not None
            ]
        else:
            # Counts/amounts: an empty bucket is a real, measured zero.
            counts: dict[datetime, list[int]] = {}
            for o in outcomes:
                key = floor_bucket(o.ts, bucket_minutes)
                entry = counts.setdefault(key, [0, 0, 0])
                entry[0] += 1
                if not o.success:
                    entry[1] += 1
                    entry[2] += o.amount_paise
            idx = {"payments_total": 0, "payments_failed": 1, "failed_amount_paise": 2}[metric]
            points = [
                TimeSeriesPoint(ts=k, value=float(v[idx]))
                for k, v in sorted(counts.items())
            ]
    return DashboardTimeseries(
        metric=metric, granularity=granularity, points=points, environment=environment
    )

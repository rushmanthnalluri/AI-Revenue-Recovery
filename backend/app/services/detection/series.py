"""Time-bucketed metric series built from the ``payment_events`` stream.

The detection engine consumes two series families:

- ``payment_success_rate`` — per bucket, share of terminal payment outcomes
  that succeeded (``captured``/``authorized`` vs ``failed``). Degrades DOWN.
- ``capture_latency_ms`` — per bucket, mean capture latency in milliseconds
  (event ``payload["latency_ms"]`` when present, else the gap between the
  payment's creation timestamp and its captured event). Degrades UP.

A payment's outcome is its *latest* terminal event inside the window — per
Razorpay semantics a ``payment.failed`` can legitimately be followed by a
``payment.captured`` for the same payment, so failures are not terminal.

Segment dimensions (``method`` / ``bank`` / ``gateway``) come from the payment
row: ``method`` is a column; ``bank`` and ``gateway`` live in ``Payment.meta``
(gateway defaults to ``"razorpay"``). These slice the series to localize a
degradation to the failing rail.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Payment, PaymentEvent

METRIC_SUCCESS_RATE = "payment_success_rate"
METRIC_CAPTURE_LATENCY = "capture_latency_ms"
KNOWN_METRICS: tuple[str, ...] = (METRIC_SUCCESS_RATE, METRIC_CAPTURE_LATENCY)

# Metric direction: which way is "degraded".
METRIC_DIRECTION: dict[str, str] = {
    METRIC_SUCCESS_RATE: "down",
    METRIC_CAPTURE_LATENCY: "up",
}

SUCCESS_STATUSES = ("captured", "authorized")
FAILURE_STATUSES = ("failed",)
TERMINAL_STATUSES = SUCCESS_STATUSES + FAILURE_STATUSES

SEGMENT_DIMENSIONS: tuple[str, ...] = ("method", "bank", "gateway")
UNKNOWN_SEGMENT = "unknown"


@dataclass(frozen=True)
class Bucket:
    """One time bucket of a metric series. ``value`` is None when the bucket
    has no usable events (detectors skip those)."""

    ts: datetime  # bucket start, tz-aware UTC
    value: float | None
    count: int  # events contributing to this bucket


@dataclass(frozen=True)
class PaymentOutcome:
    """One payment's resolved terminal outcome inside the analysis window."""

    payment_id: str
    ts: datetime  # when the outcome was decided (terminal event time)
    success: bool
    amount_paise: int
    latency_ms: float | None
    segments: dict[str, str] = field(default_factory=dict)


def floor_bucket(ts: datetime, bucket_minutes: int) -> datetime:
    """Floor a tz-aware datetime to its bucket boundary (UTC)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    epoch = int(ts.timestamp())
    size = bucket_minutes * 60
    return datetime.fromtimestamp(epoch - (epoch % size), tz=timezone.utc)


def payment_segments(payment: Payment) -> dict[str, str]:
    meta = payment.meta or {}
    return {
        "method": payment.method or UNKNOWN_SEGMENT,
        "bank": str(meta.get("bank") or UNKNOWN_SEGMENT),
        "gateway": str(meta.get("gateway") or "razorpay"),
    }


def latest_event_anchor(db: Session) -> datetime | None:
    """Most recent terminal event time — the default right edge of a window.

    Anchoring to the data (not wall-clock now) makes detection runs
    deterministic and idempotent: the same data yields the same window.
    """
    return db.scalar(
        sa.select(sa.func.max(PaymentEvent.occurred_at)).where(
            PaymentEvent.to_status.in_(TERMINAL_STATUSES)
        )
    )


def load_outcomes(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    segment: dict[str, str] | None = None,
) -> list[PaymentOutcome]:
    """Resolve one terminal outcome per payment inside the window.

    Optionally restricted to a segment slice, e.g. ``{"method": "upi"}``.
    """
    stmt = (
        sa.select(PaymentEvent, Payment)
        .join(Payment, PaymentEvent.payment_id == Payment.id)
        .where(
            PaymentEvent.occurred_at >= window_start,
            PaymentEvent.occurred_at <= window_end,
            PaymentEvent.to_status.in_(TERMINAL_STATUSES),
        )
        .order_by(PaymentEvent.occurred_at.asc())
    )
    rows = db.execute(stmt).all()

    # Latest terminal event per payment wins (failed -> captured happens).
    by_payment: dict[str, PaymentOutcome] = {}
    for event, payment in rows:
        segments = payment_segments(payment)
        latency_ms = _event_latency_ms(event, payment)
        by_payment[payment.id] = PaymentOutcome(
            payment_id=payment.id,
            ts=event.occurred_at,
            success=event.to_status in SUCCESS_STATUSES,
            amount_paise=payment.amount_paise,
            latency_ms=latency_ms,
            segments=segments,
        )

    outcomes = list(by_payment.values())
    if segment:
        outcomes = [
            o
            for o in outcomes
            if all(o.segments.get(dim, UNKNOWN_SEGMENT) == val for dim, val in segment.items())
        ]
    return outcomes


def _event_latency_ms(event: PaymentEvent, payment: Payment) -> float | None:
    if event.to_status not in SUCCESS_STATUSES:
        return None
    payload = event.payload or {}
    raw = payload.get("latency_ms")
    if isinstance(raw, (int, float)) and raw >= 0:
        return float(raw)
    start = payment.gateway_created_at or payment.created_at
    if start is None:
        return None
    delta_ms = (event.occurred_at - start).total_seconds() * 1000.0
    return max(delta_ms, 0.0)


def build_series(
    outcomes: list[PaymentOutcome],
    *,
    metric: str,
    window_start: datetime,
    window_end: datetime,
    bucket_minutes: int,
) -> list[Bucket]:
    """Aggregate outcomes into a fixed grid of buckets covering the window."""
    if metric not in KNOWN_METRICS:
        raise ValueError(f"unknown metric: {metric!r} (known: {sorted(KNOWN_METRICS)})")

    start = floor_bucket(window_start, bucket_minutes)
    step = timedelta(minutes=bucket_minutes)
    n_buckets = max(1, int((window_end - start).total_seconds() // (bucket_minutes * 60)) + 1)

    successes = [0] * n_buckets
    totals = [0] * n_buckets
    latency_sum = [0.0] * n_buckets
    latency_n = [0] * n_buckets

    for o in outcomes:
        idx = int((floor_bucket(o.ts, bucket_minutes) - start).total_seconds() // (bucket_minutes * 60))
        if not 0 <= idx < n_buckets:
            continue
        if metric == METRIC_SUCCESS_RATE:
            totals[idx] += 1
            if o.success:
                successes[idx] += 1
        else:  # capture_latency_ms
            if o.latency_ms is not None:
                latency_sum[idx] += o.latency_ms
                latency_n[idx] += 1

    buckets: list[Bucket] = []
    for i in range(n_buckets):
        ts = start + i * step
        if metric == METRIC_SUCCESS_RATE:
            value = (successes[i] / totals[i]) if totals[i] else None
            count = totals[i]
        else:
            value = (latency_sum[i] / latency_n[i]) if latency_n[i] else None
            count = latency_n[i]
        buckets.append(Bucket(ts=ts, value=value, count=count))
    return buckets


def slice_outcomes(
    outcomes: list[PaymentOutcome], dimension: str
) -> dict[str, list[PaymentOutcome]]:
    """Group outcomes by one segment dimension (for localization)."""
    groups: dict[str, list[PaymentOutcome]] = {}
    for o in outcomes:
        groups.setdefault(o.segments.get(dimension, UNKNOWN_SEGMENT), []).append(o)
    return groups

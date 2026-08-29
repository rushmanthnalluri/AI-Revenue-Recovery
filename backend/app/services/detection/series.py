"""Time-bucketed metric series built from the ``payment_events`` stream.

The detection engine consumes four series families:

- ``payment_success_rate`` — per bucket, share of terminal payment outcomes
  that succeeded (``captured``/``authorized`` vs ``failed``). Degrades DOWN.
- ``capture_latency_ms`` — per bucket, mean capture latency in milliseconds
  (event ``payload["latency_ms"]`` when present, else the gap between the
  payment's creation timestamp and its captured event). Degrades UP.
- ``checkout_abandonment_rate`` — per bucket, share of checkout attempts
  (payments *created* in the bucket) that never reached a terminal outcome
  within an inactivity threshold of creation. Degrades UP. Attempt-based:
  abandoned checkouts stay ``created`` forever, so outcome-based series are
  structurally blind to them. Right-censoring is handled honestly: an attempt
  is only *decidable* once ``created + threshold`` is inside the window; still
  undecidable attempts are excluded from numerator and denominator.
- ``insufficient_fund_share`` — per bucket, share of *failed* terminal
  outcomes whose error reason is insufficient funds. Degrades UP. Built for
  the small-volume regime (night traffic), where the success rate itself
  carries too few events to score: the signal is the *mix* of failures, not
  their count.

A payment's outcome is its *latest* terminal event inside the window — per
Razorpay semantics a ``payment.failed`` can legitimately be followed by a
``payment.captured`` for the same payment, so failures are not terminal.

Segment dimensions (``method`` / ``bank`` / ``gateway`` / ``route``) come from
the payment row: ``method`` is a column; ``bank``, ``gateway`` and ``route``
live in ``Payment.meta`` (gateway defaults to ``"razorpay"``). These slice the
series to localize a degradation to the failing rail.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Payment, PaymentEvent

METRIC_SUCCESS_RATE = "payment_success_rate"
METRIC_CAPTURE_LATENCY = "capture_latency_ms"
METRIC_CHECKOUT_ABANDONMENT = "checkout_abandonment_rate"
METRIC_INSUFFICIENT_FUND_SHARE = "insufficient_fund_share"
KNOWN_METRICS: tuple[str, ...] = (
    METRIC_SUCCESS_RATE,
    METRIC_CAPTURE_LATENCY,
    METRIC_CHECKOUT_ABANDONMENT,
    METRIC_INSUFFICIENT_FUND_SHARE,
)

# Metric direction: which way is "degraded".
METRIC_DIRECTION: dict[str, str] = {
    METRIC_SUCCESS_RATE: "down",
    METRIC_CAPTURE_LATENCY: "up",
    METRIC_CHECKOUT_ABANDONMENT: "up",
    METRIC_INSUFFICIENT_FUND_SHARE: "up",
}

#: Metrics built from checkout *attempts* (payments created in the window,
#: whether or not they ever reached a terminal state) instead of resolved
#: terminal outcomes. Abandoned checkouts never become terminal outcomes, so
#: outcome-based series are blind to them by construction.
ATTEMPT_BASED_METRICS: tuple[str, ...] = (METRIC_CHECKOUT_ABANDONMENT,)

SUCCESS_STATUSES = ("captured", "authorized")
FAILURE_STATUSES = ("failed",)
TERMINAL_STATUSES = SUCCESS_STATUSES + FAILURE_STATUSES

SEGMENT_DIMENSIONS: tuple[str, ...] = ("method", "bank", "gateway", "route")
UNKNOWN_SEGMENT = "unknown"


def is_insufficient_fund(reason: object) -> bool:
    """Substring match on the normalized failure reason — mirrors the
    defensive normalization of the revenue failure classifier (which this
    leaf package may not import): Razorpay telemetry has no closed enum."""
    if not isinstance(reason, str) or not reason.strip():
        return False
    norm = reason.strip().lower().replace("-", "_").replace(" ", "_")
    return "insufficient_fund" in norm or "insufficient_balance" in norm


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
    # failure reason of the deciding event (None for successes): the raw
    # error_reason telemetry, matched defensively downstream (no closed enum).
    error_reason: str | None = None


@dataclass(frozen=True)
class CheckoutAttempt:
    """One payment *created* inside the analysis window, resolved or not.

    ``abandoned`` is tri-state vs the inactivity threshold:
    - ``True``  — no terminal outcome within ``created + threshold`` (stuck);
    - ``False`` — resolved within the threshold;
    - ``None``  — right-censored: the threshold horizon falls beyond the
      pass's knowledge edge, so the attempt is undecidable and must be
      excluded from both the numerator and the denominator.
    """

    payment_id: str
    ts: datetime  # creation time (gateway_created_at, else row created_at)
    resolved_ts: datetime | None  # first terminal event at/before the horizon
    amount_paise: int
    segments: dict[str, str] = field(default_factory=dict)
    abandoned: bool | None = None


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
        "route": str(meta.get("route") or UNKNOWN_SEGMENT),
    }


def latest_event_anchor(db: Session, source_types: tuple[str, ...] | None = None) -> datetime | None:
    """Most recent terminal event time — the default right edge of a window.

    Anchoring to the data (not wall-clock now) makes detection runs
    deterministic and idempotent: the same data yields the same window.
    ``source_types`` restricts the anchor to one environment's commerce rows
    (the real_test/research boundary — a pass must never anchor on the other
    environment's events).
    """
    stmt = sa.select(sa.func.max(PaymentEvent.occurred_at)).where(
        PaymentEvent.to_status.in_(TERMINAL_STATUSES)
    )
    if source_types is not None:
        stmt = stmt.join(Payment, PaymentEvent.payment_id == Payment.id).where(
            Payment.source_type.in_(source_types)
        )
    return db.scalar(stmt)


def load_outcomes(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    segment: dict[str, str] | None = None,
    source_types: tuple[str, ...] | None = None,
) -> list[PaymentOutcome]:
    """Resolve one terminal outcome per payment inside the window.

    Optionally restricted to a segment slice, e.g. ``{"method": "upi"}``, and
    to an environment via ``source_types`` (payments whose ``source_type`` is
    in the environment's set — see app.models.base.source_types_for_environment).
    Failures carry the deciding event's ``error_reason`` (payload first, then
    the payment row) for the error-share metric.
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
    if source_types is not None:
        stmt = stmt.where(Payment.source_type.in_(source_types))
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
            error_reason=_event_error_reason(event, payment),
        )

    outcomes = list(by_payment.values())
    if segment:
        outcomes = [
            o
            for o in outcomes
            if all(o.segments.get(dim, UNKNOWN_SEGMENT) == val for dim, val in segment.items())
        ]
    return outcomes


def load_checkout_attempts(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    segment: dict[str, str] | None = None,
    *,
    inactivity_minutes: int = 30,
    source_types: tuple[str, ...] | None = None,
) -> list[CheckoutAttempt]:
    """Resolve every payment *created* in the window against the inactivity
    threshold, as of the pass's knowledge edge (``window_end``).

    A terminal event after ``window_end`` is the future from the pass's point
    of view and is never consulted. ``abandoned`` is tri-state (see
    :class:`CheckoutAttempt`): attempts whose threshold horizon falls beyond
    the window edge are right-censored (``None``), not counted as abandoned.
    ``source_types`` restricts the pass to one environment's payments.
    """
    created_col = sa.func.coalesce(Payment.gateway_created_at, Payment.created_at)
    stmt = (
        sa.select(Payment)
        .where(created_col >= window_start, created_col < window_end)
        .order_by(created_col.asc())
    )
    if source_types is not None:
        stmt = stmt.where(Payment.source_type.in_(source_types))
    payments = list(db.scalars(stmt))
    if not payments:
        return []

    resolved: dict[str, datetime] = {}
    ids = [p.id for p in payments]
    for i in range(0, len(ids), 500):  # stay under SQLite's variable limit
        rows = db.execute(
            sa.select(PaymentEvent.payment_id, sa.func.min(PaymentEvent.occurred_at))
            .where(
                PaymentEvent.payment_id.in_(ids[i : i + 500]),
                PaymentEvent.to_status.in_(TERMINAL_STATUSES),
                PaymentEvent.occurred_at <= window_end,
            )
            .group_by(PaymentEvent.payment_id)
        ).all()
        resolved.update(rows)

    threshold = timedelta(minutes=inactivity_minutes)
    attempts: list[CheckoutAttempt] = []
    for p in payments:
        created = p.gateway_created_at or p.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        resolved_ts = resolved.get(p.id)
        if resolved_ts is not None and resolved_ts.tzinfo is None:
            resolved_ts = resolved_ts.replace(tzinfo=timezone.utc)
        horizon = created + threshold
        if horizon > window_end:
            abandoned: bool | None = None  # right-censored at the pass edge
        else:
            abandoned = resolved_ts is None or resolved_ts > horizon
        attempts.append(
            CheckoutAttempt(
                payment_id=p.id,
                ts=created,
                resolved_ts=resolved_ts,
                amount_paise=p.amount_paise,
                segments=payment_segments(p),
                abandoned=abandoned,
            )
        )
    if segment:
        attempts = [
            a
            for a in attempts
            if all(a.segments.get(dim, UNKNOWN_SEGMENT) == val for dim, val in segment.items())
        ]
    return attempts


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


def _event_error_reason(event: PaymentEvent, payment: Payment) -> str | None:
    """Failure reason carried by the deciding terminal event (failures only):
    event payload first, then the payment row's preserved telemetry."""
    if event.to_status in SUCCESS_STATUSES:
        return None
    payload = event.payload or {}
    meta = payment.meta or {}
    for raw in (
        payload.get("error_reason"),
        meta.get("error_reason"),
        payload.get("error_code"),
        payment.error_code,
    ):
        if isinstance(raw, str) and raw.strip():
            return raw
    return None


def build_series(
    outcomes: list[PaymentOutcome],
    *,
    metric: str,
    window_start: datetime,
    window_end: datetime,
    bucket_minutes: int,
) -> list[Bucket]:
    """Aggregate outcomes into a fixed grid of buckets covering the window."""
    if metric in ATTEMPT_BASED_METRICS:
        raise ValueError(
            f"{metric!r} is attempt-based: use build_abandonment_series "
            "with load_checkout_attempts records"
        )
    if metric not in KNOWN_METRICS:
        raise ValueError(f"unknown metric: {metric!r} (known: {sorted(KNOWN_METRICS)})")

    start = floor_bucket(window_start, bucket_minutes)
    step = timedelta(minutes=bucket_minutes)
    n_buckets = max(1, int((window_end - start).total_seconds() // (bucket_minutes * 60)) + 1)

    successes = [0] * n_buckets
    totals = [0] * n_buckets
    latency_sum = [0.0] * n_buckets
    latency_n = [0] * n_buckets
    failures = [0] * n_buckets
    insufficient = [0] * n_buckets

    for o in outcomes:
        idx = int((floor_bucket(o.ts, bucket_minutes) - start).total_seconds() // (bucket_minutes * 60))
        if not 0 <= idx < n_buckets:
            continue
        if metric == METRIC_SUCCESS_RATE:
            totals[idx] += 1
            if o.success:
                successes[idx] += 1
        elif metric == METRIC_CAPTURE_LATENCY:
            if o.latency_ms is not None:
                latency_sum[idx] += o.latency_ms
                latency_n[idx] += 1
        else:  # insufficient_fund_share: the failure MIX, not the count
            if not o.success:
                failures[idx] += 1
                if is_insufficient_fund(o.error_reason):
                    insufficient[idx] += 1

    buckets: list[Bucket] = []
    for i in range(n_buckets):
        ts = start + i * step
        if metric == METRIC_SUCCESS_RATE:
            value = (successes[i] / totals[i]) if totals[i] else None
            count = totals[i]
        elif metric == METRIC_CAPTURE_LATENCY:
            value = (latency_sum[i] / latency_n[i]) if latency_n[i] else None
            count = latency_n[i]
        else:
            value = (insufficient[i] / failures[i]) if failures[i] else None
            count = failures[i]
        buckets.append(Bucket(ts=ts, value=value, count=count))
    return buckets


def build_abandonment_series(
    attempts: list[CheckoutAttempt],
    *,
    window_start: datetime,
    window_end: datetime,
    bucket_minutes: int,
) -> list[Bucket]:
    """Aggregate checkout attempts into the abandonment-rate grid.

    Per bucket: share of *decidable* attempts created in the bucket that were
    abandoned (no terminal outcome within the inactivity threshold). Bucket
    ``count`` is the decidable-attempt count — censored attempts
    (``abandoned is None``) are excluded entirely, so buckets near the pass's
    knowledge edge simply carry less signal instead of reading unresolved
    attempts as abandoned.
    """
    start = floor_bucket(window_start, bucket_minutes)
    step = timedelta(minutes=bucket_minutes)
    n_buckets = max(1, int((window_end - start).total_seconds() // (bucket_minutes * 60)) + 1)

    decided = [0] * n_buckets
    abandoned = [0] * n_buckets
    for a in attempts:
        if a.abandoned is None:
            continue  # right-censored
        idx = int((floor_bucket(a.ts, bucket_minutes) - start).total_seconds() // (bucket_minutes * 60))
        if not 0 <= idx < n_buckets:
            continue
        decided[idx] += 1
        if a.abandoned:
            abandoned[idx] += 1

    return [
        Bucket(
            ts=start + i * step,
            value=(abandoned[i] / decided[i]) if decided[i] else None,
            count=decided[i],
        )
        for i in range(n_buckets)
    ]


def build_metric_series(
    records: list,
    *,
    metric: str,
    window_start: datetime,
    window_end: datetime,
    bucket_minutes: int,
) -> list[Bucket]:
    """Dispatch on the metric family: attempt-based metrics consume
    ``CheckoutAttempt`` records, the rest consume ``PaymentOutcome`` records."""
    if metric in ATTEMPT_BASED_METRICS:
        return build_abandonment_series(
            records,
            window_start=window_start,
            window_end=window_end,
            bucket_minutes=bucket_minutes,
        )
    return build_series(
        records,
        metric=metric,
        window_start=window_start,
        window_end=window_end,
        bucket_minutes=bucket_minutes,
    )


def slice_outcomes(
    outcomes: list,
    dimension: str,
) -> dict[str, list]:
    """Group records by one segment dimension (for localization). Works for
    both ``PaymentOutcome`` and ``CheckoutAttempt`` (anything with
    ``.segments``)."""
    groups: dict[str, list] = {}
    for o in outcomes:
        groups.setdefault(o.segments.get(dimension, UNKNOWN_SEGMENT), []).append(o)
    return groups

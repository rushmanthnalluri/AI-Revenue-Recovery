"""Facet-tagged terminal outcomes from the ``payment_events`` stream.

Outcome semantics deliberately mirror detection's (the *latest* terminal event
inside the window decides — ``failed`` can be followed by ``captured``), and
the window convention mirrors diagnosis's (an equal-duration baseline
immediately preceding the incident window). Both are reimplemented here rather
than imported so the insights package stays a leaf reader under ADR 0010.

Facet sources:
- ``method``  — Payment.method (event payload fallback), else "unknown".
- ``bank``    — Payment.meta["bank"] (payload fallback), else "unknown".
- ``gateway`` — Payment.meta["gateway"], default "razorpay".
- ``error_code``   — Payment.error_code (payload fallback); failures only.
- ``error_reason`` — Payment.meta["error_reason"] (payload fallback); failures only.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Payment, PaymentEvent

SUCCESS_STATUSES = ("captured", "authorized")
FAILURE_STATUSES = ("failed",)
TERMINAL_STATUSES = SUCCESS_STATUSES + FAILURE_STATUSES

# Facets present on every payment -> within-group failure-rate comparison.
GROUP_DIMENSIONS: tuple[str, ...] = ("method", "bank", "gateway")
# Facets present only on failures -> share-of-failures comparison.
ERROR_DIMENSIONS: tuple[str, ...] = ("error_code", "error_reason")

UNKNOWN = "unknown"
DEFAULT_GATEWAY = "razorpay"


@dataclass(frozen=True)
class FacetOutcome:
    """One payment's resolved terminal outcome in a window, with its facets."""

    payment_id: str
    success: bool
    facets: dict[str, str] = field(default_factory=dict)


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip().lower()
    return s or None


def incident_windows(incident: Any) -> tuple[datetime, datetime, datetime, datetime]:
    """Return (baseline_start, baseline_end, window_start, window_end).

    The baseline is the equal-duration window immediately preceding the
    incident window. Falls back to a 1-hour window ending at ``detected_at``
    when the incident carries no explicit window.
    """
    end = incident.window_end or incident.detected_at
    start = incident.window_start or (end - timedelta(hours=1))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    duration = end - start
    if duration.total_seconds() <= 0:
        raise ValueError(f"incident {incident.id}: non-positive window {start}..{end}")
    return start - duration, start, start, end


def load_outcomes(
    session: Session,
    start: datetime,
    end: datetime,
    source_types: tuple[str, ...] | None = None,
) -> list[FacetOutcome]:
    """Resolve one terminal outcome per payment inside ``[start, end)``.

    ``source_types`` restricts the pass to one environment's payments (the
    real_test/research boundary — see
    app.models.base.source_types_for_environment).
    """
    stmt = (
        sa.select(Payment, PaymentEvent)
        .join(PaymentEvent, PaymentEvent.payment_id == Payment.id)
        .where(
            PaymentEvent.occurred_at >= start,
            PaymentEvent.occurred_at < end,
            PaymentEvent.to_status.in_(TERMINAL_STATUSES),
        )
        .order_by(PaymentEvent.occurred_at.asc(), PaymentEvent.id.asc())
    )
    if source_types is not None:
        stmt = stmt.where(Payment.source_type.in_(source_types))
    rows = session.execute(stmt).all()

    # Latest terminal event per payment wins (failed -> captured happens).
    latest: dict[str, tuple[Payment, PaymentEvent]] = {}
    for payment, event in rows:
        latest[payment.id] = (payment, event)

    outcomes: list[FacetOutcome] = []
    for payment, event in latest.values():
        meta = payment.meta or {}
        payload = event.payload or {}
        success = event.to_status in SUCCESS_STATUSES
        facets = {
            "method": _norm(payment.method or payload.get("method")) or UNKNOWN,
            "bank": _norm(meta.get("bank") or payload.get("bank")) or UNKNOWN,
            "gateway": _norm(meta.get("gateway")) or DEFAULT_GATEWAY,
            "error_code": UNKNOWN,
            "error_reason": UNKNOWN,
        }
        if not success:
            facets["error_code"] = (
                _norm(payment.error_code or payload.get("error_code")) or UNKNOWN
            )
            facets["error_reason"] = (
                _norm(meta.get("error_reason") or payload.get("error_reason")) or UNKNOWN
            )
        outcomes.append(
            FacetOutcome(payment_id=payment.id, success=success, facets=facets)
        )
    return outcomes


def restrict_to_segment(
    outcomes: list[FacetOutcome], segment: dict[str, str] | None
) -> list[FacetOutcome]:
    """Keep only outcomes matching the incident's segment slice (if any)."""
    if not segment:
        return outcomes
    wanted = {dim: _norm(val) for dim, val in segment.items()}
    return [
        o
        for o in outcomes
        if all(o.facets.get(dim, UNKNOWN) == val for dim, val in wanted.items())
    ]

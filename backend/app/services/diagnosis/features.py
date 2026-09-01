"""Feature engineering for incident root-cause diagnosis.

The unit of analysis is one *incident window* ``[window_start, window_end)``
compared against a *baseline window* of equal duration immediately preceding
it. From the ``payment_events`` stream (joined to ``payments``) each payment is
normalized into a plain record dict:

    outcome:         "failed" | "captured" | "pending"  (pending = no terminal
                     state inside the window — the abandonment proxy)
    method:          upi | card | netbanking | wallet | emi | ... | None
    bank:            issuer/acquirer bank tag when known (defensive: payload
                     ``bank``, ``card.network``, or payment meta)
    error_source:    gateway | bank | customer | network | issuer | ... | None
    error_step:      payment_authentication | payment_authorization | ... | None
    error_reason:    insufficient_fund | payment_timed_out | ... | None
    latency_ms:      gateway latency when the source recorded it, else None
    is_subscription: True when the payment is a subscription charge
    amount_paise:    integer paise (INR)

``compute_features`` maps (window records, baseline records) to a fixed-length
numeric vector (``FEATURE_NAMES``). All values are plain floats so the dict is
JSON-serializable and can be stored on the diagnoses row unchanged.

Delta conventions:
- For dimensions that exist on *all* payments (method, bank) we use the
  within-group failure-rate delta: fr_window(v) - fr_baseline(v).
- For dimensions that exist only on *failed* payments (error_source/step/
  reason) within-group rates are meaningless, so we use the share-of-failures
  delta: share_window(v) - share_baseline(v).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import numpy as np
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Payment, PaymentEvent

# ---------------------------------------------------------------------------
# Canonical categorical values tracked as explicit features. Everything else
# still flows into the max/top aggregates, just without a dedicated column.
# ---------------------------------------------------------------------------

METHODS = ("upi", "card", "netbanking", "wallet", "emi")
ERROR_SOURCES = ("gateway", "bank", "customer")
ERROR_STEPS = ("payment_authentication", "payment_authorization")
TRACKED_REASONS = (
    "insufficient_fund",
    "payment_timed_out",
    "gateway_technical_error",
    "bank_technical_error",
    "incorrect_otp",
    "payment_cancelled",
    "card_declined",
    "authentication_failed",
    "transaction_limit_exceeded",
)
DELTA_REASONS = (
    "insufficient_fund",
    "bank_technical_error",
    "gateway_technical_error",
    "payment_timed_out",
)

GROUP_RATE_DIMS = ("method", "bank")  # within-group failure-rate deltas
GROUP_SHARE_DIMS = ("error_source", "error_step", "error_reason")  # share-of-failures deltas

TERMINAL_SUCCESS = {"captured", "authorized"}
TERMINAL_FAILURE = {"failed"}

FEATURE_NAMES: list[str] = [
    # volume / headline rates
    "volume",
    "volume_delta_ratio",
    "failed_volume",
    "failure_rate_w",
    "failure_rate_b",
    "failure_rate_delta",
    # per-method
    "max_method_rate_delta",
    "top_method_fail_share",
    *[f"top_method_{m}" for m in METHODS],
    # per-bank
    "max_bank_rate_delta",
    "top_bank_fail_share",
    "distinct_failing_banks_w",
    # error source
    "max_source_share_delta",
    "top_source_fail_share",
    *[f"top_source_{s}" for s in ERROR_SOURCES],
    *[f"src_fail_share_w_{s}" for s in ERROR_SOURCES],
    *[f"src_fail_share_delta_{s}" for s in ERROR_SOURCES],
    # error step
    "max_step_share_delta",
    "top_step_fail_share",
    *[f"top_step_{s}" for s in ERROR_STEPS],
    # error reason
    "max_reason_share_delta",
    "top_reason_fail_share",
    *[f"reason_share_w_{r}" for r in TRACKED_REASONS],
    *[f"reason_share_delta_{r}" for r in DELTA_REASONS],
    # latency
    "latency_coverage",
    "latency_p50_w",
    "latency_p90_w",
    "latency_p50_delta",
    "latency_p90_delta",
    "latency_p90_delta_ratio",
    # abandonment proxy
    "abandonment_rate_w",
    "abandonment_rate_delta",
    # subscriptions
    "sub_share_w",
    "sub_failure_share_w",
    "sub_failure_share_delta",
    "sub_failure_rate_delta",
]

assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES)), "duplicate feature names"

Record = dict[str, Any]


# ---------------------------------------------------------------------------
# Record extraction from the payment_events stream
# ---------------------------------------------------------------------------

def _norm_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip().lower()
    return s or None


def load_window_records(session: Session, start: datetime, end: datetime) -> list[Record]:
    """Normalize one record per payment that has any event in ``[start, end)``.

    The latest event in the window decides the outcome and carries the error
    telemetry (Razorpay webhook payloads put error_source/step/reason on the
    payment entity, mirrored into the event payload — see docs/research.md).
    """
    rows = session.execute(
        sa.select(Payment, PaymentEvent)
        .join(PaymentEvent, PaymentEvent.payment_id == Payment.id)
        .where(PaymentEvent.occurred_at >= start, PaymentEvent.occurred_at < end)
        .order_by(PaymentEvent.occurred_at.asc())
    ).all()

    latest: dict[str, tuple[Payment, PaymentEvent]] = {}
    for payment, event in rows:
        latest[payment.id] = (payment, event)  # ascending order -> last wins

    records: list[Record] = []
    for payment, event in latest.values():
        payload = event.payload or {}
        meta = payment.meta or {}
        to_status = _norm_str(event.to_status) or _norm_str(payment.status)
        if to_status in TERMINAL_FAILURE:
            outcome = "failed"
        elif to_status in TERMINAL_SUCCESS:
            outcome = "captured"
        else:
            outcome = "pending"

        card = payload.get("card") if isinstance(payload.get("card"), dict) else {}
        latency = payload.get("latency_ms", meta.get("latency_ms"))
        records.append(
            {
                "outcome": outcome,
                "method": _norm_str(payment.method or payload.get("method")),
                "bank": _norm_str(
                    payload.get("bank") or card.get("network") or meta.get("bank")
                ),
                "error_source": _norm_str(payload.get("error_source") or payment.error_source),
                "error_step": _norm_str(payload.get("error_step")),
                "error_reason": _norm_str(payload.get("error_reason")),
                "latency_ms": float(latency) if latency is not None else None,
                "is_subscription": bool(meta.get("subscription_id") or payload.get("subscription_id")),
                "amount_paise": int(payment.amount_paise or 0),
            }
        )
    return records


def incident_windows(incident: Any) -> tuple[datetime, datetime, datetime, datetime]:
    """Return (baseline_start, baseline_end, window_start, window_end).

    Falls back to a 1-hour window ending at ``detected_at`` when the incident
    carries no explicit window.
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


def compute_features_for_incident(
    session: Session,
    incident: Any,
    window: tuple[datetime, datetime] | None = None,
) -> dict[str, float]:
    """DB-backed feature computation used by DiagnosisService.classify().

    ``window`` overrides the incident's persisted frame — the re-scoping
    triage (rescope.py) passes its tightened span here; None keeps the
    historical behavior (the incident's own window). The baseline stays an
    equal-duration span immediately preceding the scored frame, matching the
    exact-span frames the model was selected on."""
    if window is None:
        b_start, b_end, w_start, w_end = incident_windows(incident)
    else:
        w_start, w_end = window
        duration = w_end - w_start
        if duration.total_seconds() <= 0:
            raise ValueError(f"incident {incident.id}: non-positive window {w_start}..{w_end}")
        b_start, b_end = w_start - duration, w_start
    window_records = load_window_records(session, w_start, w_end)
    baseline_records = load_window_records(session, b_start, b_end)
    return compute_features(window_records, baseline_records)


# ---------------------------------------------------------------------------
# Pure feature computation (DB-free — shared by training and inference)
# ---------------------------------------------------------------------------

def _failed(records: Iterable[Record]) -> list[Record]:
    return [r for r in records if r.get("outcome") == "failed"]


def _rate(num: int, den: int) -> float:
    return float(num) / float(den) if den > 0 else 0.0


def _rate_delta(w_recs: list[Record], b_recs: list[Record], dim: str) -> tuple[float, str | None, float]:
    """Max within-group failure-rate delta over a dimension present on all
    payments. Returns (max_delta, top_value, top_value_share_of_window_failures)."""
    w_fail, b_fail = _failed(w_recs), _failed(b_recs)
    values = {r.get(dim) for r in w_recs + b_recs} - {None}
    best_delta, best_val = 0.0, None
    for v in values:
        nw = sum(1 for r in w_recs if r.get(dim) == v)
        nb = sum(1 for r in b_recs if r.get(dim) == v)
        fw = sum(1 for r in w_fail if r.get(dim) == v)
        fb = sum(1 for r in b_fail if r.get(dim) == v)
        delta = _rate(fw, nw) - _rate(fb, nb)
        if delta > best_delta:
            best_delta, best_val = delta, v
    top_share = _rate(sum(1 for r in w_fail if r.get(dim) == best_val), len(w_fail)) if best_val else 0.0
    return best_delta, best_val, top_share


def _share_delta(w_recs: list[Record], b_recs: list[Record], dim: str) -> tuple[float, str | None, float]:
    """Max share-of-failures delta for a dimension present only on failures."""
    w_fail, b_fail = _failed(w_recs), _failed(b_recs)
    values = {r.get(dim) for r in w_fail + b_fail} - {None}
    best_delta, best_val = 0.0, None
    for v in values:
        sw = _rate(sum(1 for r in w_fail if r.get(dim) == v), len(w_fail))
        sb = _rate(sum(1 for r in b_fail if r.get(dim) == v), len(b_fail))
        if sw - sb > best_delta:
            best_delta, best_val = sw - sb, v
    top_share = _rate(sum(1 for r in w_fail if r.get(dim) == best_val), len(w_fail)) if best_val else 0.0
    return best_delta, best_val, top_share


def _share(records: list[Record], dim: str, value: str) -> float:
    return _rate(sum(1 for r in records if r.get(dim) == value), len(records))


def _pctl(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=float), q)) if values else 0.0


def compute_features(window_records: list[Record], baseline_records: list[Record]) -> dict[str, float]:
    """Compute the fixed diagnosis feature vector for one incident window."""
    w, b = window_records, baseline_records
    w_fail, b_fail = _failed(w), _failed(b)
    nw, nb = len(w), len(b)

    f: dict[str, float] = {}

    # Volume / headline rates ------------------------------------------------
    f["volume"] = float(nw)
    f["volume_delta_ratio"] = (nw - nb) / max(nb, 1)
    f["failed_volume"] = float(len(w_fail))
    f["failure_rate_w"] = _rate(len(w_fail), nw)
    f["failure_rate_b"] = _rate(len(b_fail), nb)
    f["failure_rate_delta"] = f["failure_rate_w"] - f["failure_rate_b"]

    # Per-method -------------------------------------------------------------
    delta, top, share = _rate_delta(w, b, "method")
    f["max_method_rate_delta"] = delta
    f["top_method_fail_share"] = share
    for m in METHODS:
        f[f"top_method_{m}"] = 1.0 if top == m else 0.0

    # Per-bank ---------------------------------------------------------------
    delta, top, share = _rate_delta(w, b, "bank")
    f["max_bank_rate_delta"] = delta
    f["top_bank_fail_share"] = share
    f["distinct_failing_banks_w"] = float(len({r.get("bank") for r in w_fail} - {None}))

    # Error source -----------------------------------------------------------
    delta, top, share = _share_delta(w, b, "error_source")
    f["max_source_share_delta"] = delta
    f["top_source_fail_share"] = share
    for s in ERROR_SOURCES:
        f[f"top_source_{s}"] = 1.0 if top == s else 0.0
        f[f"src_fail_share_w_{s}"] = _share(w_fail, "error_source", s)
        f[f"src_fail_share_delta_{s}"] = _share(w_fail, "error_source", s) - _share(
            b_fail, "error_source", s
        )

    # Error step -------------------------------------------------------------
    delta, top, share = _share_delta(w, b, "error_step")
    f["max_step_share_delta"] = delta
    f["top_step_fail_share"] = share
    for s in ERROR_STEPS:
        f[f"top_step_{s}"] = 1.0 if top == s else 0.0

    # Error reason -----------------------------------------------------------
    delta, top, share = _share_delta(w, b, "error_reason")
    f["max_reason_share_delta"] = delta
    f["top_reason_fail_share"] = share
    for r in TRACKED_REASONS:
        f[f"reason_share_w_{r}"] = _share(w_fail, "error_reason", r)
    for r in DELTA_REASONS:
        f[f"reason_share_delta_{r}"] = _share(w_fail, "error_reason", r) - _share(
            b_fail, "error_reason", r
        )

    # Latency ----------------------------------------------------------------
    lat_w = [r["latency_ms"] for r in w if r.get("latency_ms") is not None]
    lat_b = [r["latency_ms"] for r in b if r.get("latency_ms") is not None]
    p50_w, p90_w = _pctl(lat_w, 50), _pctl(lat_w, 90)
    p50_b, p90_b = _pctl(lat_b, 50), _pctl(lat_b, 90)
    f["latency_coverage"] = _rate(len(lat_w), nw)
    f["latency_p50_w"] = p50_w
    f["latency_p90_w"] = p90_w
    f["latency_p50_delta"] = p50_w - p50_b
    f["latency_p90_delta"] = p90_w - p90_b
    f["latency_p90_delta_ratio"] = (p90_w - p90_b) / max(p90_b, 1.0)

    # Abandonment proxy ------------------------------------------------------
    pend_w = sum(1 for r in w if r.get("outcome") == "pending")
    pend_b = sum(1 for r in b if r.get("outcome") == "pending")
    f["abandonment_rate_w"] = _rate(pend_w, nw)
    f["abandonment_rate_delta"] = _rate(pend_w, nw) - _rate(pend_b, nb)

    # Subscriptions ----------------------------------------------------------
    subs_w = [r for r in w if r.get("is_subscription")]
    subs_b = [r for r in b if r.get("is_subscription")]
    f["sub_share_w"] = _rate(len(subs_w), nw)
    f["sub_failure_share_w"] = _rate(len(_failed(subs_w)), len(w_fail))
    f["sub_failure_share_delta"] = _rate(len(_failed(subs_w)), len(w_fail)) - _rate(
        len(_failed(subs_b)), len(b_fail)
    )
    f["sub_failure_rate_delta"] = _rate(len(_failed(subs_w)), len(subs_w)) - _rate(
        len(_failed(subs_b)), len(subs_b)
    )

    # Guarantee the exact, JSON-serializable feature contract.
    return {name: float(f.get(name, 0.0)) for name in FEATURE_NAMES}


def features_to_vector(features: dict[str, float], names: list[str] | None = None) -> list[float]:
    """Order a feature dict as a model input vector (missing keys -> 0.0)."""
    names = names or FEATURE_NAMES
    return [float(features.get(n, 0.0)) for n in names]


__all__ = [
    "FEATURE_NAMES",
    "METHODS",
    "ERROR_SOURCES",
    "ERROR_STEPS",
    "TRACKED_REASONS",
    "Record",
    "load_window_records",
    "incident_windows",
    "compute_features_for_incident",
    "compute_features",
    "features_to_vector",
]

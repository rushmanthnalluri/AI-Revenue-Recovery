"""Failure-class classification for payments.

Razorpay failure telemetry (error_code / error_description / error_source,
plus error_reason when the ingest kept it in `meta`) has no closed canonical
enum — the docs themselves are inconsistent across pages (see
docs/research.md). So classification is deliberately defensive: normalized
substring matching against a documented pattern table, never strict enum
equality, and an explicit UNKNOWN class when nothing matches.

The classes are recoverability classes, not a taxonomy of every reason —
they answer "how winnable is this failure?", which is what the revenue
engine multiplies against.
"""

from enum import Enum
from typing import Any, Mapping


class FailureClass(str, Enum):
    TIMEOUT = "timeout"  # payment_timed_out — transient rail hiccup
    SOFT_DECLINE = "soft_decline"  # gateway/bank technical errors, generic declines
    HARD_DECLINE = "hard_decline"  # invalid/disabled instrument, permanent auth failure
    INSUFFICIENT_FUNDS = "insufficient_funds"  # balance not there right now
    ABANDONMENT = "abandonment"  # customer cancelled / failed customer-side auth
    UNKNOWN = "unknown"  # no classifiable signal


# Substring patterns per class, checked in dict order — first matching class
# wins, so more specific classes precede generic ones. Patterns are matched
# against the lowercase, separator-normalized haystack (see _normalize).
_REASON_PATTERNS: dict[FailureClass, tuple[str, ...]] = {
    FailureClass.TIMEOUT: (
        "payment_timed_out",
        "timed_out",
        "timeout",
    ),
    FailureClass.INSUFFICIENT_FUNDS: (
        "insufficient_fund",
        "insufficient_balance",
    ),
    FailureClass.HARD_DECLINE: (
        "card_number_invalid",
        "invalid_card",
        "card_disabled",
        "lost_card",
        "stolen_card",
        "pickup_card",
        "revocation_of_authorization",
        "authentication_failed",
        "pin_attempts_exceeded",
        "debit_instrument_blocked",
    ),
    FailureClass.ABANDONMENT: (
        "payment_cancelled",
        "cancelled_by_customer",
        "customer_cancelled",
        "incorrect_otp",
        "incorrect_pin",
        "otp",
    ),
    FailureClass.SOFT_DECLINE: (
        "card_declined",
        "payment_declined",
        "gateway_technical_error",
        "bank_technical_error",
        "technical_error",
        "gateway_error",
        "transaction_limit_exceeded",
        "duplicate_request",
        "temporarily_unavailable",
        "server_error",
    ),
}

# When no reason pattern matches, error_source is a weak hint: an
# infrastructure-side failure is usually transient (soft), a customer-side
# failure usually means the session died (abandonment). Documented as a
# fallback only — reason patterns always win.
_SOURCE_FALLBACK: dict[str, FailureClass] = {
    "gateway": FailureClass.SOFT_DECLINE,
    "bank": FailureClass.SOFT_DECLINE,
    "issuer": FailureClass.SOFT_DECLINE,
    "network": FailureClass.SOFT_DECLINE,
    "razorpay": FailureClass.SOFT_DECLINE,
    "customer": FailureClass.ABANDONMENT,
}


def _normalize(value: str) -> str:
    """Lowercase and unify separators so 'Payment Timed Out', 'payment-timed-out'
    and 'payment_timed_out' all match the same pattern."""
    return (
        value.strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
        .replace(".", "_")
    )


def classify_reason(
    *,
    error_code: str | None = None,
    error_description: str | None = None,
    error_source: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> FailureClass:
    """Classify a failure from raw error fields. Never raises on odd input."""
    haystacks: list[str] = []
    for raw in (error_code, error_description):
        if isinstance(raw, str) and raw.strip():
            haystacks.append(_normalize(raw))
    if meta:
        # Ingest may have preserved the richer Razorpay fields in meta.
        for key in ("error_reason", "reason"):
            val = meta.get(key)
            if isinstance(val, str) and val.strip():
                haystacks.append(_normalize(val))

    for cls, patterns in _REASON_PATTERNS.items():
        if any(pattern in hay for pattern in patterns for hay in haystacks):
            return cls

    if isinstance(error_source, str) and error_source.strip():
        fallback = _SOURCE_FALLBACK.get(_normalize(error_source))
        if fallback is not None:
            return fallback

    return FailureClass.UNKNOWN


def classify_failure(payment: Any) -> FailureClass:
    """Classify a Payment ORM row (duck-typed: reads attributes defensively)."""
    meta = getattr(payment, "meta", None)
    return classify_reason(
        error_code=getattr(payment, "error_code", None),
        error_description=getattr(payment, "error_description", None),
        error_source=getattr(payment, "error_source", None),
        meta=meta if isinstance(meta, Mapping) else None,
    )

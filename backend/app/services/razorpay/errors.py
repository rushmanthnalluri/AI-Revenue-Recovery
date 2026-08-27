"""Typed gateway errors + mapping from the Razorpay error envelope.

Razorpay error responses look like:

    {"error": {"code": "BAD_REQUEST_ERROR", "description": "...",
               "source": "customer", "step": "payment_authentication",
               "reason": "incorrect_otp", "metadata": {...}, "field": "amount"}}

Mapping policy (drives the recovery-action state machine):

- 4xx (`GatewayClientError` subclasses): the request was REJECTED before
  processing — the outcome is known (nothing happened). Safe to surface to the
  policy/execution layer as a definitive failure.
- 5xx (`GatewayServerError`) and network/timeout (`GatewayTransientError`):
  the request may or may not have been processed — the outcome is AMBIGUOUS.
  The execution layer must mark the action UNKNOWN, never blind-retry a
  mutating call, and resolve by re-querying fetch_payment/fetch_order.
- `GatewayRateLimitError` (429): rejected before processing; retryable later
  even for mutating calls, but the adapter itself never retries mutations.
"""

from typing import Any


class GatewayError(Exception):
    """Base for all gateway errors. Carries the Razorpay error taxonomy."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        source: str | None = None,
        step: str | None = None,
        reason: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.source = source
        self.step = step
        self.reason = reason
        self.raw = raw or {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"{type(self).__name__}(status={self.status_code}, code={self.code!r}, "
            f"reason={self.reason!r}, message={self.message!r})"
        )


# --- definitive failures (request rejected, nothing happened) ---------------


class GatewayClientError(GatewayError):
    """4xx base: the gateway rejected the request before processing it."""


class GatewayBadRequestError(GatewayClientError):
    """400 — validation failure, duplicate receipt/reference_id, bad state."""


class GatewayAuthenticationError(GatewayClientError):
    """401/403 — bad or unauthorized API keys."""


class GatewayNotFoundError(GatewayClientError):
    """404 — entity does not exist (or wrong account/mode)."""


class GatewayRateLimitError(GatewayClientError):
    """429 — rejected before processing; the caller may retry later."""


# --- ambiguous outcomes (request may have been processed) -------------------


class GatewayTransientError(GatewayError):
    """Timeout / connection failure / unreadable response: no authoritative
    answer was received, so the outcome of a mutating call is UNKNOWN.

    Safe to retry only for idempotent GETs (the adapter does that itself with
    exponential backoff). NEVER blind-retry a mutating call after this error.
    """


class GatewayServerError(GatewayTransientError):
    """5xx — Razorpay-side failure; the request may have been processed."""


class GatewayResponseError(GatewayTransientError):
    """2xx with a malformed/unexpected body — treat as ambiguous."""


def _extract(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the Razorpay error envelope fields out of a response payload."""
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return {"message": "unknown gateway error"}
    return {
        "message": err.get("description") or "gateway error",
        "code": err.get("code"),
        "source": err.get("source"),
        "step": err.get("step"),
        "reason": err.get("reason"),
    }


def map_error_response(status_code: int, payload: dict[str, Any]) -> GatewayError:
    """Map an HTTP error status + Razorpay error envelope to a typed error."""
    fields = _extract(payload)
    if status_code == 400:
        cls: type[GatewayError] = GatewayBadRequestError
    elif status_code in (401, 403):
        cls = GatewayAuthenticationError
    elif status_code == 404:
        cls = GatewayNotFoundError
    elif status_code == 429:
        cls = GatewayRateLimitError
    elif status_code >= 500:
        cls = GatewayServerError
    else:
        cls = GatewayClientError
    return cls(status_code=status_code, raw=payload, **fields)


__all__ = [
    "GatewayError",
    "GatewayClientError",
    "GatewayBadRequestError",
    "GatewayAuthenticationError",
    "GatewayNotFoundError",
    "GatewayRateLimitError",
    "GatewayTransientError",
    "GatewayServerError",
    "GatewayResponseError",
    "map_error_response",
]

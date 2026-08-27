"""Real Razorpay adapter — raw REST over httpx, no SDK (see docs/research.md).

Implements the `app.ports.PaymentGateway` protocol against Razorpay Test Mode
(same base URL as live; the `rzp_test_*` key selects the mode).

Idempotency reality (verified in docs/research.md):
- Orders dedupe via unique `receipt` — we send `idempotency_key` as `receipt`.
- Payment Links dedupe via unique `reference_id` — same mapping.
- Payments/Subscriptions have NO idempotency — we never retry mutating calls;
  the internal execution ledger (recovery_actions.gateway_request_id UNIQUE)
  plus UNKNOWN-resolution via fetch_payment/fetch_order is the guard.

Retry policy: exponential backoff ONLY for idempotent GETs on transient
errors (timeout / connection / 5xx / malformed response). Mutating POSTs are
sent exactly once; a transient outcome raises GatewayTransientError so the
caller can mark the action UNKNOWN and resolve by re-querying.
"""

import hashlib
import hmac
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.logging import get_logger
from app.services.razorpay.errors import (
    GatewayResponseError,
    GatewayTransientError,
    map_error_response,
)

logger = get_logger("app.services.razorpay.client")

# Razorpay rejects receipt/reference_id longer than 40 chars.
_MAX_RECEIPT_LEN = 40


class RazorpayGateway:
    """PaymentGateway implementation backed by the real Razorpay REST API."""

    def __init__(
        self,
        *,
        key_id: str,
        key_secret: str,
        base_url: str = "https://api.razorpay.com/v1",
        webhook_secret: str = "",
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.25,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not key_id or not key_secret:
            raise ValueError("RazorpayGateway requires key_id and key_secret")
        self._webhook_secret = webhook_secret
        self._max_retries = max(1, max_retries)
        self._backoff_base = backoff_base_seconds
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            auth=(key_id, key_secret),  # HTTP Basic: key_id:key_secret
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={"Content-Type": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------
    # PaymentGateway protocol
    # ------------------------------------------------------------------

    def create_order(
        self,
        *,
        amount_paise: int,
        currency: str = "INR",
        receipt: str | None = None,
        notes: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        # `receipt` is the order-level dedupe key; the internal ledger id wins.
        effective_receipt = (receipt or idempotency_key or "")[:_MAX_RECEIPT_LEN] or None
        body: dict[str, Any] = {"amount": amount_paise, "currency": currency}
        if effective_receipt:
            body["receipt"] = effective_receipt
        if notes:
            body["notes"] = notes
        return self._request("POST", "orders", body=body, idempotent=False)

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"orders/{order_id}", idempotent=True)

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        return self._request("GET", f"payments/{payment_id}", idempotent=True)

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        currency: str = "INR",
        customer: dict[str, Any] | None = None,
        description: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"amount": amount_paise, "currency": currency}
        if idempotency_key:
            # `reference_id` is the payment-link dedupe key.
            body["reference_id"] = idempotency_key[:_MAX_RECEIPT_LEN]
        if customer:
            body["customer"] = customer
        if description:
            body["description"] = description
        return self._request("POST", "payment_links", body=body, idempotent=False)

    def create_subscription(
        self,
        *,
        plan_id: str,
        customer_id: str | None = None,
        total_count: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        logger.warning(
            "create_subscription has no gateway-side idempotency; protection is "
            "only the internal ledger (unique gateway_request_id) — never retrying",
            extra={"gateway_request_id": idempotency_key},
        )
        body: dict[str, Any] = {"plan_id": plan_id}
        if total_count is not None:
            body["total_count"] = total_count
        if customer_id:
            body["customer_id"] = customer_id
        if idempotency_key:
            # No dedupe field exists; notes keep the ledger id traceable.
            body["notes"] = {"gateway_request_id": idempotency_key}
        return self._request("POST", "subscriptions", body=body, idempotent=False)

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """HMAC-SHA256(webhook_secret, RAW body) vs X-Razorpay-Signature.

        Never parse/cast the body before this check; the secret is the webhook
        secret configured in the Dashboard (not necessarily the API secret).
        Fails closed when no secret is configured.
        """
        if not self._webhook_secret or not signature:
            return False
        expected = hmac.new(
            self._webhook_secret.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotent: bool,
    ) -> dict[str, Any]:
        attempts = self._max_retries if idempotent else 1
        for attempt in range(attempts):
            try:
                resp = self._http.request(method, path, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < attempts - 1:
                    self._sleep(self._backoff(attempt))
                    continue
                raise GatewayTransientError(
                    f"gateway request failed: {type(exc).__name__}: {exc}"
                ) from exc

            if resp.status_code < 400:
                return self._parse_success(resp, method, path)

            err = map_error_response(resp.status_code, self._safe_json(resp))
            # Backoff-retry transient statuses only for idempotent GETs.
            transient_status = resp.status_code == 429 or resp.status_code >= 500
            if idempotent and transient_status and attempt < attempts - 1:
                self._sleep(self._backoff(attempt))
                continue
            raise err
        raise GatewayTransientError("gateway retries exhausted")  # pragma: no cover

    def _parse_success(self, resp: httpx.Response, method: str, path: str) -> dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise GatewayResponseError(
                f"non-JSON response from {method} {path}",
                status_code=resp.status_code,
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise GatewayResponseError(
                f"malformed response from {method} {path}: missing entity id",
                status_code=resp.status_code,
                raw=payload if isinstance(payload, dict) else {},
            )
        return payload

    @staticmethod
    def _safe_json(resp: httpx.Response) -> dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _backoff(self, attempt: int) -> float:
        return self._backoff_base * (2**attempt)


__all__ = ["RazorpayGateway"]

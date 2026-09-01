"""Read-only Razorpay REST client for the merchant sync service.

The write-path adapter (`app.services.razorpay.client.RazorpayGateway`)
implements the `PaymentGateway` port (single-entity create/fetch); its
response parser requires an entity `id`, so it cannot read COLLECTION
envelopes (`{"entity":"collection","count":N,"items":[...]}`). Sync needs
exactly those list endpoints, so this client is its read-side sibling:
GET-only, HTTP Basic auth, the same typed-error mapping
(`app.services.razorpay.errors`), and backoff retries that are safe because
every call here is an idempotent GET (docs/razorpay-integration.md §B/§I).

Pagination reality (verified 2026-08-28, docs/razorpay-integration.md §A):
`from`/`to` (unix) + `count` (max 100) + `skip` on orders/payments/
subscriptions; `GET /v1/payment_links` documents only `payment_id` /
`reference_id` filters and no pagination — sync therefore fetches links by
targeted `reference_id` only.
"""

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

logger = get_logger("app.services.merchant.client")

#: Razorpay rejects `count` above 100 on every documented list endpoint.
MAX_PAGE_SIZE = 100


class RazorpayReadClient:
    """GET-only Razorpay API reader. Never logs credentials."""

    def __init__(
        self,
        *,
        key_id: str,
        key_secret: str,
        base_url: str = "https://api.razorpay.com/v1",
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.25,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not key_id or not key_secret:
            raise ValueError("RazorpayReadClient requires key_id and key_secret")
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
    # reads
    # ------------------------------------------------------------------

    def probe(self) -> None:
        """Authenticated connectivity check: `GET /v1/payments?count=1`.

        Returns None on success; raises the typed gateway error otherwise
        (401 -> GatewayAuthenticationError, network -> GatewayTransientError).
        """
        self.get_collection_page("payments", {"count": 1})

    def get_collection_page(
        self, path: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """One page of a list endpoint, returned as the raw `items` list.

        Validates the COLLECTION envelope shape only; per-entity validation
        (and quarantine) lives in `normalize.py`. `GET /v1/payment_links`
        responses are also accepted as a bare JSON array — the docs describe
        the filtered response as "an array of link objects" without
        documenting the envelope (docs/razorpay-integration.md §K).
        """
        payload = self._get(path, params)
        if isinstance(payload, list):  # payment_links filtered-response form
            items: Any = payload
        elif isinstance(payload, dict):
            items = payload.get("items")
        else:
            items = None
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise GatewayResponseError(
                f"malformed collection envelope from GET {path}: "
                "expected {'items': [...]} or a JSON array of objects",
                raw=payload if isinstance(payload, dict) else {},
            )
        return items

    # ------------------------------------------------------------------
    # plumbing (mirrors RazorpayGateway: backoff for idempotent GETs only)
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        for attempt in range(self._max_retries):
            try:
                resp = self._http.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self._max_retries - 1:
                    self._sleep(self._backoff_base * (2**attempt))
                    continue
                raise GatewayTransientError(
                    f"gateway request failed: {type(exc).__name__}: {exc}"
                ) from exc
            if resp.status_code < 400:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise GatewayResponseError(
                        f"non-JSON response from GET {path}",
                        status_code=resp.status_code,
                    ) from exc
            err = map_error_response(resp.status_code, self._safe_json(resp))
            transient = resp.status_code == 429 or resp.status_code >= 500
            if transient and attempt < self._max_retries - 1:
                self._sleep(self._backoff_base * (2**attempt))
                continue
            raise err
        raise GatewayTransientError("gateway retries exhausted")  # pragma: no cover

    @staticmethod
    def _safe_json(resp: httpx.Response) -> dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}


__all__ = ["RazorpayReadClient", "MAX_PAGE_SIZE"]

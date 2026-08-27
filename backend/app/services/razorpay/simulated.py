"""SIMULATION — SimulatedPaymentGateway. NOT A REAL PAYMENT GATEWAY.

This is the in-memory simulation twin of `RazorpayGateway`, implementing the
same `app.ports.PaymentGateway` protocol so the whole detection → policy →
execution → verification loop can run without Razorpay credentials. It is used
when `SIMULATION_MODE=true` or when no API keys are configured (see
`factory.get_gateway`). Nothing here talks to the network.

Determinism: every outcome is derived from `seed` via per-entity keyed RNGs
(`random.Random(f"{seed}:{key}")`), so the same seed reproduces byte-identical
entity payloads regardless of call order or wall clock. `base_ts` fixes the
timestamps. An optional `incident` context perturbs outcomes to simulate
degradation (gateway outage, success-rate collapse, specific error reasons).
"""

import hashlib
import hmac
import itertools
import json
import random
from typing import Any

from app.services.razorpay.errors import (
    GatewayBadRequestError,
    GatewayNotFoundError,
    GatewayTransientError,
)

DEFAULT_SEED = 20260826
DEFAULT_WEBHOOK_SECRET = "sim-webhook-secret"
# Deterministic clock origin for simulated entities (2023-11-14T22:13:20Z).
DEFAULT_BASE_TS = 1_700_000_000

_METHODS = ("upi", "card", "netbanking", "wallet")


class SimulatedPaymentGateway:
    """PaymentGateway twin with seeded, reproducible outcomes. SIMULATION ONLY."""

    def __init__(
        self,
        *,
        seed: int = DEFAULT_SEED,
        webhook_secret: str = DEFAULT_WEBHOOK_SECRET,
        success_rate: float = 0.7,
        incident: dict[str, Any] | None = None,
        base_ts: int = DEFAULT_BASE_TS,
    ) -> None:
        self._seed = seed
        self._webhook_secret = webhook_secret
        self._success_rate = success_rate
        self._incident = incident or {}
        self._base_ts = base_ts
        self._counter = itertools.count(1)
        self.orders: dict[str, dict[str, Any]] = {}
        self.payments: dict[str, dict[str, Any]] = {}
        self.payment_links: dict[str, dict[str, Any]] = {}
        self.subscriptions: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # PaymentGateway protocol (simulated)
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
        self._maybe_outage()
        effective_receipt = receipt or idempotency_key
        if effective_receipt and any(
            o.get("receipt") == effective_receipt for o in self.orders.values()
        ):
            raise GatewayBadRequestError(
                "An order with the same receipt value has already been created",
                status_code=400,
                code="BAD_REQUEST_ERROR",
                reason="input_validation_failed",
            )
        n = next(self._counter)
        order = {
            "id": f"order_sim{n:04d}",
            "entity": "order",
            "amount": amount_paise,
            "amount_paid": 0,
            "amount_due": amount_paise,
            "currency": currency,
            "receipt": effective_receipt,
            "status": "created",
            "attempts": 0,
            "notes": notes or {},
            "created_at": self._ts(n),
        }
        self.orders[order["id"]] = order
        return dict(order)

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        order = self.orders.get(order_id)
        if order is None:
            raise GatewayNotFoundError(
                f"The id {order_id} does not exist",
                status_code=404,
                code="BAD_REQUEST_ERROR",
                reason="input_validation_failed",
            )
        return dict(order)

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        payment = self.payments.get(payment_id)
        if payment is None:
            raise GatewayNotFoundError(
                f"The id {payment_id} does not exist",
                status_code=404,
                code="BAD_REQUEST_ERROR",
                reason="input_validation_failed",
            )
        return dict(payment)

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        currency: str = "INR",
        customer: dict[str, Any] | None = None,
        description: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._maybe_outage()
        if idempotency_key and any(
            link.get("reference_id") == idempotency_key
            for link in self.payment_links.values()
        ):
            raise GatewayBadRequestError(
                "An existing reference id has been passed.",
                status_code=400,
                code="BAD_REQUEST_ERROR",
                reason="input_validation_failed",
            )
        n = next(self._counter)
        link_id = f"plink_sim{n:04d}"
        paid = self._rng(f"plink_outcome:{idempotency_key or link_id}").random() < self._rate()
        link: dict[str, Any] = {
            "id": link_id,
            "entity": "payment_link",
            "amount": amount_paise,
            "amount_paid": 0,
            "currency": currency,
            "reference_id": idempotency_key,
            "status": "created",
            "short_url": f"https://rzp.io/i/sim{n:04d}",
            "customer": customer or {},
            "description": description,
            "payments": [],
            "created_at": self._ts(n),
        }
        payment = self._new_payment(
            amount_paise=amount_paise,
            currency=currency,
            key=f"plink_pay:{link_id}",
            succeeded=paid,
        )
        link["payments"] = [{"id": payment["id"], "status": payment["status"]}]
        if paid:
            link["status"] = "paid"
            link["amount_paid"] = amount_paise
        self.payment_links[link_id] = link
        return dict(link)

    def create_subscription(
        self,
        *,
        plan_id: str,
        customer_id: str | None = None,
        total_count: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._maybe_outage()
        n = next(self._counter)
        sub_id = f"sub_sim{n:04d}"
        active = self._rng(f"sub_outcome:{sub_id}").random() < self._rate()
        sub = {
            "id": sub_id,
            "entity": "subscription",
            "plan_id": plan_id,
            "customer_id": customer_id,
            "status": "active" if active else "pending",
            "total_count": total_count or 12,
            "paid_count": 0,
            "remaining_count": total_count or 12,
            "auth_attempts": 0,
            "short_url": f"https://rzp.io/i/simsub{n:04d}",
            "notes": {"gateway_request_id": idempotency_key} if idempotency_key else {},
            "created_at": self._ts(n),
        }
        self.subscriptions[sub_id] = sub
        return dict(sub)

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        if not self._webhook_secret or not signature:
            return False
        expected = hmac.new(
            self._webhook_secret.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------
    # SIMULATION helpers (not part of the PaymentGateway port)
    # ------------------------------------------------------------------

    def build_event(
        self, event_type: str, entity: dict[str, Any]
    ) -> tuple[bytes, str, str]:
        """Build a signed webhook delivery (body, signature, event id).

        SIMULATION ONLY — lets tests/demos exercise POST /webhooks/razorpay
        end to end with a genuinely valid X-Razorpay-Signature.
        """
        n = next(self._counter)
        event_id = f"evt_sim{n:06d}"
        kind = entity.get("entity", "payment")
        body = json.dumps(
            {
                "entity": "event",
                "account_id": "acc_sim",
                "event": event_type,
                "contains": [kind],
                "payload": {kind: {"entity": entity}},
                "created_at": self._ts(n),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(
            self._webhook_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return body, signature, event_id

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _rng(self, key: str) -> random.Random:
        return random.Random(f"{self._seed}:{key}")

    def _ts(self, n: int) -> int:
        return self._base_ts + n * 60

    def _rate(self) -> float:
        return float(self._incident.get("success_rate", self._success_rate))

    def _maybe_outage(self) -> None:
        if self._incident.get("outage"):
            raise GatewayTransientError(
                "simulated gateway outage: no authoritative response received",
                status_code=503,
                code="GATEWAY_ERROR",
                reason="gateway_technical_error",
            )

    def _new_payment(
        self, *, amount_paise: int, currency: str, key: str, succeeded: bool
    ) -> dict[str, Any]:
        n = next(self._counter)
        rng = self._rng(key)
        payment: dict[str, Any] = {
            "id": f"pay_sim{n:04d}",
            "entity": "payment",
            "amount": amount_paise,
            "currency": currency,
            "status": "captured" if succeeded else "failed",
            "method": rng.choice(_METHODS),
            "captured": succeeded,
            "created_at": self._ts(n),
        }
        if not succeeded:
            reason = self._incident.get("error_reason") or rng.choice(
                ("insufficient_fund", "payment_timed_out", "card_declined")
            )
            payment.update(
                {
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": f"simulated failure: {reason}",
                    "error_source": "bank",
                    "error_step": "payment_authorization",
                    "error_reason": reason,
                }
            )
        self.payments[payment["id"]] = payment
        return payment


__all__ = [
    "SimulatedPaymentGateway",
    "DEFAULT_SEED",
    "DEFAULT_WEBHOOK_SECRET",
]

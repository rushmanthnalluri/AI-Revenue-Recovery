"""Normalize raw Razorpay entities into commerce-model field dicts.

Validation policy (docs/razorpay-integration.md §I.5): check the entity
shape, required-field types, and documented enum membership; anything that
fails raises `EntityValidationError` and the sync service QUARANTINES the
entity (skips it, records it in `sync_runs.entity_counts.errors`) instead of
silently coercing or crashing the run. Field sets per §C (verified
2026-08-28).

Money stays integer paise; unix timestamps convert to tz-aware UTC at this
boundary. The raw entity is preserved under `meta["razorpay"]` so every
synced row can be reconciled against the upstream snapshot (§I.6).
"""

from datetime import datetime, timezone
from typing import Any

#: Documented status enums (docs/razorpay-integration.md §C).
ORDER_STATUSES = frozenset({"created", "attempted", "paid"})
PAYMENT_STATUSES = frozenset({"created", "authorized", "captured", "refunded", "failed"})
SUBSCRIPTION_STATUSES = frozenset(
    {"created", "authenticated", "active", "pending", "halted", "cancelled", "completed", "expired"}
)
PAYMENT_LINK_STATUSES = frozenset({"created", "partially_paid", "expired", "cancelled", "paid"})


class EntityValidationError(ValueError):
    """A raw entity failed validation -> quarantine it, never crash the run."""


def _require_id(raw: dict[str, Any]) -> str:
    entity_id = raw.get("id")
    if not isinstance(entity_id, str) or not entity_id:
        raise EntityValidationError("missing or non-string 'id'")
    return entity_id


def _require_amount(raw: dict[str, Any]) -> int:
    amount = raw.get("amount")
    # JSON booleans are ints in Python; neither `true` nor floats are paise.
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
        raise EntityValidationError("missing or invalid integer 'amount' (paise)")
    return amount


def _require_status(raw: dict[str, Any], allowed: frozenset[str]) -> str:
    status = raw.get("status")
    if not isinstance(status, str) or not status:
        raise EntityValidationError("missing or non-string 'status'")
    if status not in allowed:
        raise EntityValidationError(f"undocumented status {status!r}")
    return status


def _require_currency(raw: dict[str, Any]) -> str:
    currency = raw.get("currency")
    if not isinstance(currency, str) or not currency:
        raise EntityValidationError("missing or non-string 'currency'")
    return currency


def _unix_ts(raw: dict[str, Any], field: str = "created_at") -> datetime:
    value = raw.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise EntityValidationError(f"missing or non-numeric {field!r} (unix timestamp)")
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _opt_str(raw: dict[str, Any], field: str, limit: int = 255) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    return str(value)[:limit]


def _opt_int(raw: dict[str, Any], field: str) -> int | None:
    value = raw.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_unix(raw: dict[str, Any], field: str) -> datetime | None:
    value = raw.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _notes(raw: dict[str, Any]) -> dict[str, Any]:
    notes = raw.get("notes")
    if isinstance(notes, dict):
        return notes
    if isinstance(notes, list):  # Razorpay returns [] for empty notes
        return {}
    return {}


def normalize_order(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """-> (gateway_order_id, Order field dict). merchant_id is set by the caller."""
    order_id = _require_id(raw)
    created_at = _unix_ts(raw)
    fields: dict[str, Any] = {
        "gateway_order_id": order_id,
        "amount_paise": _require_amount(raw),
        "currency": _require_currency(raw),
        "status": _require_status(raw, ORDER_STATUSES),
        "receipt": _opt_str(raw, "receipt", 128),
        "created_at": created_at,
        "meta": {
            "razorpay": raw,
            "amount_paid_paise": _opt_int(raw, "amount_paid"),
            "amount_due_paise": _opt_int(raw, "amount_due"),
            "attempts": _opt_int(raw, "attempts"),
            "notes": _notes(raw),
        },
    }
    return order_id, fields


def normalize_payment(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """-> (gateway_payment_id, Payment field dict).

    merchant_id/order_id are resolved by the caller (local FKs). The error
    quintet (§C) is carried verbatim: code/description/source as columns,
    step/reason in meta.
    """
    payment_id = _require_id(raw)
    created_at = _unix_ts(raw)
    fields = {
        "gateway_payment_id": payment_id,
        "amount_paise": _require_amount(raw),
        "currency": _require_currency(raw),
        "status": _require_status(raw, PAYMENT_STATUSES),
        "method": _opt_str(raw, "method", 32),
        "error_code": _opt_str(raw, "error_code", 64),
        "error_description": _opt_str(raw, "error_description", 2000),
        "error_source": _opt_str(raw, "error_source", 32),
        "captured": raw.get("captured") is True,
        "attempts": 0,  # attempts live on the ORDER entity in Razorpay's model
        "gateway_created_at": created_at,
        "created_at": created_at,
        "meta": {
            "razorpay": raw,
            "email": _opt_str(raw, "email"),
            "contact": _opt_str(raw, "contact"),
            "fee_paise": _opt_int(raw, "fee"),
            "tax_paise": _opt_int(raw, "tax"),
            "refund_status": _opt_str(raw, "refund_status", 16),
            "amount_refunded_paise": _opt_int(raw, "amount_refunded"),
            "international": raw.get("international") is True,
            "error_step": _opt_str(raw, "error_step", 64),
            "error_reason": _opt_str(raw, "error_reason", 255),
            "notes": _notes(raw),
        },
    }
    # Gateway order id (if any) — the caller maps it to the local Order row.
    order_id = raw.get("order_id")
    if isinstance(order_id, str) and order_id:
        fields["_gateway_order_id"] = order_id
    return payment_id, fields


def normalize_payment_link(raw: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Validate a payment-link entity -> (link_id, embedded payment entities).

    There is deliberately no payment_links table: links are OUR outbound
    recovery instruments (reconciled via webhooks). Sync validates the link,
    counts it, and harvests the `payments[]` sub-array (populated only after
    capture, §C) so a link-paid payment lands in the payments table even if
    its window/list page missed it.
    """
    link_id = _require_id(raw)
    _require_status(raw, PAYMENT_LINK_STATUSES)
    payments = raw.get("payments")
    if payments is None:
        return link_id, []
    if not isinstance(payments, list):
        raise EntityValidationError("'payments' is present but not an array")
    return link_id, [p for p in payments if isinstance(p, dict)]


def normalize_subscription(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """-> (gateway_subscription_id, Subscription field dict).

    The subscription entity carries NO amount (the plan does; §C). The model
    column is non-nullable, so amount_paise is stored as 0 with
    `meta["amount_unknown"]=True` — never presented as a real amount.
    """
    subscription_id = _require_id(raw)
    created_at = _unix_ts(raw)
    fields = {
        "gateway_subscription_id": subscription_id,
        "plan_id": _opt_str(raw, "plan_id", 64),
        "status": _require_status(raw, SUBSCRIPTION_STATUSES),
        "amount_paise": 0,
        "currency": "INR",
        "period": None,  # plan attribute, not present on the entity
        "current_period_start": _opt_unix(raw, "current_start"),
        "current_period_end": _opt_unix(raw, "current_end"),
        "retry_count": 0,
        "created_at": created_at,
        "meta": {
            "razorpay": raw,
            "amount_unknown": True,
            "quantity": _opt_int(raw, "quantity"),
            "total_count": _opt_int(raw, "total_count"),
            "paid_count": _opt_int(raw, "paid_count"),
            "remaining_count": _opt_int(raw, "remaining_count"),
            "auth_attempts": _opt_int(raw, "auth_attempts"),
            "source": _opt_str(raw, "source", 16),
            "notes": _notes(raw),
        },
    }
    return subscription_id, fields


__all__ = [
    "EntityValidationError",
    "normalize_order",
    "normalize_payment",
    "normalize_payment_link",
    "normalize_subscription",
    "ORDER_STATUSES",
    "PAYMENT_STATUSES",
    "PAYMENT_LINK_STATUSES",
    "SUBSCRIPTION_STATUSES",
]

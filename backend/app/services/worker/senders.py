"""NotificationSender implementations (the `app.ports.NotificationSender` port).

- `LoggingNotificationSender` — the default. Simulated delivery: the
  notification is logged and the outbox row is marked SENT with
  `delivered_via="logging"`. No external side effects; safe in every
  environment, including the demo and tests.
- `RazorpayNotesNotificationSender` — the real-environment SEAM, selected
  with WORKER_NOTIFICATION_SENDER=razorpay_notes. Razorpay exposes no
  standalone "notify a customer" API in the PaymentGateway port, so this
  sender performs no external delivery today: it emits a structured,
  provenance-tagged receipt (`simulated: true`) so a real_test deployment
  can tell seam deliveries apart, and a live SMS/email/provider integration
  drops in behind the same port without touching the outbox or the worker.
"""

from typing import Any

from app.config import Settings
from app.logging import get_logger
from app.ports import NotificationSender

logger = get_logger(__name__)


class NotificationDeliveryError(RuntimeError):
    """Transient delivery failure — the worker retries with backoff."""


class LoggingNotificationSender:
    """Default sender: simulated delivery via the structured log."""

    name = "logging"

    def send(
        self,
        *,
        customer: dict[str, Any] | None,
        channel: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        logger.info(
            "notification delivered (simulated logging sender)",
            extra={
                "channel": channel,
                "customer_id": (customer or {}).get("id"),
                "action_id": (payload or {}).get("action_id"),
                "opportunity_id": (payload or {}).get("opportunity_id"),
            },
        )
        return {"via": self.name, "channel": channel}


class RazorpayNotesNotificationSender:
    """Real-environment seam (see module docstring). Performs no external
    delivery; the receipt's `simulated` flag keeps the provenance honest."""

    name = "razorpay_notes"

    def send(
        self,
        *,
        customer: dict[str, Any] | None,
        channel: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        logger.info(
            "notification recorded via razorpay-notes seam (no external delivery)",
            extra={
                "channel": channel,
                "customer_id": (customer or {}).get("id"),
                "action_id": (payload or {}).get("action_id"),
                "opportunity_id": (payload or {}).get("opportunity_id"),
            },
        )
        return {"via": self.name, "channel": channel, "simulated": True}


def default_sender(settings: Settings) -> NotificationSender:
    """Select the sender from configuration; unknown values fall back to the
    safe simulated default (never fail-closed into dropping notifications)."""
    selected = (settings.WORKER_NOTIFICATION_SENDER or "logging").strip().lower()
    if selected == "razorpay_notes":
        return RazorpayNotesNotificationSender()
    if selected != "logging":
        logger.warning(
            "unknown WORKER_NOTIFICATION_SENDER; falling back to logging sender",
            extra={"configured": selected},
        )
    return LoggingNotificationSender()


__all__ = [
    "LoggingNotificationSender",
    "NotificationDeliveryError",
    "RazorpayNotesNotificationSender",
    "default_sender",
]

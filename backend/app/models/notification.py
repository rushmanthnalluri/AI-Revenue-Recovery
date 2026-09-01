"""Notification outbox: queued customer notifications delivered by the
in-process worker (docs/worker.md, ADR 0011 P2).

The recovery executor enqueues one row when a `notify_customer` action fires;
the worker delivers it via the `NotificationSender` port and marks the row
SENT with provenance (`delivered_via`), or FAILED after the attempt budget is
exhausted. Every enqueue/delivery/failure lands in `audit_logs` with the
row's environment stamped.
"""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app import ids
from app.db import Base, TZDateTime, utcnow
from app.models.base import EnvironmentMixin, TimestampMixin, enum_col
from app.ports import NotificationStatus


class NotificationOutbox(EnvironmentMixin, TimestampMixin, Base):
    """One queued customer notification.

    Environment is stamped from the recovery action that requested the
    notification (real_test vs research provenance is never mixed).
    """

    __tablename__ = "notification_outbox"

    id: Mapped[str] = mapped_column(
        sa.String(64), primary_key=True, default=lambda: ids.new_id("ntf_")
    )
    # The recovery action whose firing queued this notification.
    action_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("recovery_actions.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    # Free-form channel label from the strategy constraints ("notification",
    # "email", "sms", ...); the sender interprets it.
    channel: Mapped[str] = mapped_column(
        sa.String(32), default="notification", nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    status: Mapped[NotificationStatus] = enum_col(
        NotificationStatus, "notification_status",
        default=NotificationStatus.PENDING, nullable=False, index=True,
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    # Earliest time the worker may attempt delivery (backoff pushes it out).
    due_at: Mapped[datetime] = mapped_column(
        TZDateTime(), default=utcnow, nullable=False, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    # Delivery provenance: the sender's receipt `via` ("logging",
    # "razorpay_notes", a real provider name, ...).
    delivered_via: Mapped[str | None] = mapped_column(sa.String(64))
    last_error: Mapped[str | None] = mapped_column(sa.Text)


__all__ = ["NotificationOutbox"]

"""System domain: audit trail and inbound webhook event log."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app import ids
from app.db import Base, TZDateTime
from app.models.base import TimestampMixin


class AuditLog(Base):
    """Append-only audit trail. No updated_at — rows are immutable."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        sa.Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.audit_id)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    actor: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    request_id: Mapped[str | None] = mapped_column(sa.String(64))


class WebhookEvent(TimestampMixin, Base):
    """Raw inbound webhook events. gateway_event_id is UNIQUE — that constraint
    is the dedup mechanism (Razorpay retries deliver the same event id)."""

    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.webhook_event_id)
    gateway_event_id: Mapped[str] = mapped_column(sa.String(128), unique=True, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    processed: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    error: Mapped[str | None] = mapped_column(sa.Text)
    # razorpay | simulator
    source: Mapped[str] = mapped_column(sa.String(32), default="razorpay", nullable=False)
    received_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False, index=True)

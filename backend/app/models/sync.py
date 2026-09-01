"""Sync domain: real-ingestion bookkeeping for the Razorpay Test Mode sync
service (a later wave builds the agent against EXACTLY these tables).

- ``sync_runs``: one row per sync pass over the real Razorpay API (what was
  pulled, by whom, how it ended). Append-only history.
- ``connection_state``: the singleton connection cursor for the merchant's
  real connection (id is always ``'merchant'``) — last sync/webhook
  timestamps and whether sync is enabled at all.

These rows are REAL_DATABASE operational state, not commerce data; they carry
no provenance/environment columns because they describe the connection
itself, not payments.
"""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app import ids
from app.db import Base, TZDateTime, utcnow

#: Singleton primary key for connection_state (one merchant per deployment).
CONNECTION_STATE_SINGLETON_ID = "merchant"


class SyncRun(Base):
    """One real-ingestion sync pass ( Razorpay Test Mode API -> database )."""

    __tablename__ = "sync_runs"

    id: Mapped[str] = mapped_column(
        sa.String(64), primary_key=True, default=lambda: ids.new_id("sr_")
    )
    started_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    # running | completed | failed. Python default + server_default mirror
    # migration b4e7a1c2d305 (same pattern as EnvironmentMixin.environment).
    status: Mapped[str] = mapped_column(
        sa.String(16), default="running", server_default="running", nullable=False
    )
    # per-entity pull counts, e.g. {"payments": 42, "orders": 40}
    entity_counts: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, default=dict, server_default="{}", nullable=False
    )
    error: Mapped[str | None] = mapped_column(sa.Text)
    actor: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    request_id: Mapped[str | None] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, nullable=False)


class ConnectionState(Base):
    """Singleton cursor for the merchant's real Razorpay connection."""

    __tablename__ = "connection_state"

    id: Mapped[str] = mapped_column(
        sa.String(64), primary_key=True, default=CONNECTION_STATE_SINGLETON_ID
    )
    sync_enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    last_webhook_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    # status of the most recent sync run: completed | failed | None (never ran)
    last_sync_status: Mapped[str | None] = mapped_column(sa.String(16))
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )

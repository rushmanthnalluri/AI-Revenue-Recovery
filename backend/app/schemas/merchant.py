"""Merchant connection schemas — the REAL Razorpay connection surface.

Secret hygiene: these shapes can never carry the key secret. The only
credential material exposed is `key_id_masked` (e.g. `rzp_test_••••ab12`).
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ConnectionStatus(BaseModel):
    """GET /api/v1/merchant/connection — live probe + persisted sync cursor."""

    configured: bool  # real keys present AND SIMULATION_MODE off
    connected: bool  # authenticated GET /v1/payments?count=1 succeeded
    environment: Literal["test", "live"] | None = None
    key_id_masked: str | None = None
    webhook_configured: bool  # RAZORPAY_WEBHOOK_SECRET is set
    sync_enabled: bool
    last_sync_at: datetime | None = None
    last_webhook_at: datetime | None = None
    last_sync_status: str | None = None  # completed | failed | None
    # Typed probe outcome when connected=false:
    # 'authentication_failed' (401) | 'unreachable' (network/5xx) | 'gateway_error'
    connection_error: str | None = None


class SyncRunView(BaseModel):
    """POST /api/v1/merchant/sync — the sync_runs summary of the pass."""

    id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str  # completed | failed
    entity_counts: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    actor: str
    request_id: str | None = None
    created_at: datetime


class SyncToggleView(BaseModel):
    """POST /api/v1/merchant/sync/enable|disable — Disconnect/Reconnect."""

    sync_enabled: bool
    updated_at: datetime


__all__ = ["ConnectionStatus", "SyncRunView", "SyncToggleView"]

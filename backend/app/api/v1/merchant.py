"""Merchant connection API — the REAL Razorpay Test Mode merchant surface.

- `GET  /api/v1/merchant/connection`  live connection probe + sync cursor.
- `POST /api/v1/merchant/sync`        synchronous full sync (X-API-Key via
  middleware); 409 when the real connection is not configured or sync is
  disabled; otherwise returns the `sync_runs` summary (status may be
  `completed` or `failed` — the row is the durable record).
- `POST /api/v1/merchant/sync/enable` / `.../disable` — Disconnect/Reconnect;
  audited to `audit_logs` (the router is the composition root, so the audit
  write lives here, not in the service).

`get_sync_service` is THE sync-service seam: tests override it via
`app.dependency_overrides` to inject a MockTransport-backed service.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.logging import request_id_ctx
from app.models import ENVIRONMENT_REAL_TEST
from app.schemas.merchant import ConnectionStatus, SyncRunView, SyncToggleView
from app.services.merchant import (
    SyncDisabledError,
    SyncNotConfiguredError,
    SyncService,
)
from app.services.policy import audit

router = APIRouter(prefix="/api/v1/merchant", tags=["merchant"])


def get_sync_service() -> SyncService:
    """FastAPI dependency seam — tests override this with a fixed service."""
    return SyncService.from_settings(settings)


def _connection_status(db: Session, service: SyncService) -> ConnectionStatus:
    probe = service.probe_connection()
    state = service.get_connection_state(db)
    return ConnectionStatus(
        configured=probe.configured,
        connected=probe.connected,
        environment=probe.environment,  # type: ignore[arg-type]
        key_id_masked=probe.key_id_masked,
        webhook_configured=bool(settings.RAZORPAY_WEBHOOK_SECRET),
        sync_enabled=state.sync_enabled,
        last_sync_at=state.last_sync_at,
        last_webhook_at=state.last_webhook_at,
        last_sync_status=state.last_sync_status,
        connection_error=probe.connection_error,
    )


@router.get("/connection", response_model=ConnectionStatus)
def get_connection(
    db: Session = Depends(get_db),
    service: SyncService = Depends(get_sync_service),
) -> ConnectionStatus:
    return _connection_status(db, service)


@router.post("/sync", response_model=SyncRunView)
def post_sync(
    window_days: int = Query(default=30, ge=1, le=180),
    db: Session = Depends(get_db),
    service: SyncService = Depends(get_sync_service),
) -> SyncRunView:
    try:
        run = service.run_sync(
            db,
            actor="api:merchant_sync",
            request_id=request_id_ctx.get(),
            window_days=window_days,
        )
    except SyncNotConfiguredError as exc:
        raise HTTPException(409, f"razorpay_not_configured: {exc}") from None
    except SyncDisabledError as exc:
        raise HTTPException(409, f"sync_disabled: {exc}") from None
    db.commit()
    return SyncRunView(
        id=run.id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status,
        entity_counts=run.entity_counts,
        error=run.error,
        actor=run.actor,
        request_id=run.request_id,
        created_at=run.created_at,
    )


def _set_sync_enabled(db: Session, service: SyncService, enabled: bool) -> SyncToggleView:
    state = service.set_sync_enabled(db, enabled)
    entry = audit.record(
        db,
        actor="api:merchant",
        action="merchant.sync_enable" if enabled else "merchant.sync_disable",
        entity_type="connection_state",
        entity_id=state.id,
        details={"sync_enabled": enabled},
    )
    # The connection singleton is the REAL Razorpay Test Mode merchant — the
    # toggle is real_test-only, never research.
    entry.environment = ENVIRONMENT_REAL_TEST
    db.commit()
    return SyncToggleView(sync_enabled=state.sync_enabled, updated_at=state.updated_at)


@router.post("/sync/enable", response_model=SyncToggleView)
def post_sync_enable(
    db: Session = Depends(get_db),
    service: SyncService = Depends(get_sync_service),
) -> SyncToggleView:
    """Reconnect: allow POST /sync again."""
    return _set_sync_enabled(db, service, True)


@router.post("/sync/disable", response_model=SyncToggleView)
def post_sync_disable(
    db: Session = Depends(get_db),
    service: SyncService = Depends(get_sync_service),
) -> SyncToggleView:
    """Disconnect: POST /sync refuses with 409 until re-enabled. Inbound
    webhooks are unaffected (they authenticate by signature, not by flag)."""
    return _set_sync_enabled(db, service, False)


__all__ = ["router", "get_sync_service"]

"""Generic, append-only audit helper over the shared `audit_logs` table.

Any module may record a row:

    from app.services.policy import audit

    audit.record(
        session,
        actor="human:ops@example.com",
        action="recovery.approve",
        entity_type="recovery_action",
        entity_id=action.id,
        details={"note": "looks safe"},
        request_id="req-123",          # optional; falls back to the request-id
    )                                  # contextvar set by RequestIdMiddleware

Contract:
- `record()` adds the row and flushes (so `entry.id` is usable immediately)
  but NEVER commits — the caller owns the transaction boundary.
- Rows are immutable by convention: there is deliberately no update/delete
  helper here. `audit_logs` is an append-only trail.
- `details` is coerced to a JSON-safe dict (non-serializable values become
  strings) so auditing can never crash the audited operation.
"""

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.db import utcnow
from app.logging import request_id_ctx
from app.models import AuditLog


def _jsonable(value: Any) -> Any:
    import json

    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditLog:
    """Append one immutable audit row. Flushes; does not commit."""
    if request_id is None:
        request_id = request_id_ctx.get()
    entry = AuditLog(
        actor=str(actor)[:128] if actor is not None else "unknown",
        action=str(action)[:128],
        entity_type=str(entity_type)[:64],
        entity_id=str(entity_id)[:64],
        details=_jsonable(details) if details else {},
        request_id=str(request_id)[:64] if request_id else None,
        created_at=utcnow(),
    )
    session.add(entry)
    session.flush()
    return entry


__all__ = ["record"]

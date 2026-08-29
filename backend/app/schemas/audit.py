"""Audit schemas — query the append-only audit trail."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import Paginated


class AuditLogEntry(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    actor: str
    action: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None
    created_at: datetime
    # --- additive (backwards compatible) ---
    environment: str = "research"  # real_test | research


class AuditListResponse(Paginated[AuditLogEntry]):
    pass

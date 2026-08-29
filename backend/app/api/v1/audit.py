"""Audit endpoints — read-only, newest-first window over the append-only
`audit_logs` trail written by app.services.policy.audit.record.

The `environment` filter (default 'real_test') scopes the trail to one
environment: research rows (demo.reset, simulator-derived recovery) can never
surface in a real_test query."""

import sqlalchemy as sa
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AuditLog
from app.schemas.audit import AuditListResponse, AuditLogEntry

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("", response_model=AuditListResponse)
def list_audit(
    db: Session = Depends(get_db),
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    environment: Literal["real_test", "research"] = Query(default="real_test"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> AuditListResponse:
    filters = [AuditLog.environment == environment]
    if entity_type:
        filters.append(AuditLog.entity_type == entity_type)
    if entity_id:
        filters.append(AuditLog.entity_id == entity_id)

    total = int(
        db.scalar(sa.select(sa.func.count()).select_from(AuditLog).where(*filters)) or 0
    )
    rows = db.scalars(
        sa.select(AuditLog)
        .where(*filters)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return AuditListResponse(
        items=[
            AuditLogEntry(
                id=row.id,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                actor=row.actor,
                action=row.action,
                details=dict(row.details or {}),
                request_id=row.request_id,
                created_at=row.created_at,
                environment=row.environment or "research",
            )
            for row in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )

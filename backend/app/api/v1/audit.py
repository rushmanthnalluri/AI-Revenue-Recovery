"""Audit endpoints — read-only, newest-first window over the append-only
`audit_logs` trail written by app.services.policy.audit.record.

The `environment` filter (default 'real_test') scopes the trail to one
environment: research rows (demo.reset, simulator-derived recovery) can never
surface in a real_test query. 'all' is a third, additive value: rows stamped
environment='all' (unfiltered policy-backtest runs, see api/v1/policy.py)
belong to no single environment and are queryable only via ?environment=all.
"""

import sqlalchemy as sa
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AuditLog
from app.schemas.audit import AuditListResponse, AuditLogEntry
from app.services.audit import verify_chain

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("", response_model=AuditListResponse)
def list_audit(
    db: Session = Depends(get_db),
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    environment: Literal["real_test", "research", "all"] = Query(default="real_test"),
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


class AuditVerifyResponse(BaseModel):
    valid: bool
    checked: int  # rows examined, including the first bad row
    chained: int  # examined rows carrying hashes
    legacy: int  # examined pre-chain rows (NULL hashes) — legacy-valid
    first_bad_id: str | None = None


@router.get("/verify", response_model=AuditVerifyResponse)
def verify_audit_chain(db: Session = Depends(get_db)) -> AuditVerifyResponse:
    """Read-only full-chain verification of the hash-chained audit trail.

    Walks the whole table in chain order, recomputing digests and checking
    linkage; returns the verdict plus the id of the first row that fails.
    Deliberately NOT environment-scoped: the chain spans both environments in
    insertion order, so scoping would break linkage (see
    app.services.audit.verify)."""
    report = verify_chain(db)
    return AuditVerifyResponse(
        valid=report.valid,
        checked=report.checked,
        chained=report.chained,
        legacy=report.legacy,
        first_bad_id=report.first_bad_id,
    )

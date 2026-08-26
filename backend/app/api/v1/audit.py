"""Audit endpoints. Owner: audit/observability agent."""

from fastapi import APIRouter, Query

from app.schemas.audit import AuditListResponse

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("", response_model=AuditListResponse)
def list_audit(
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> AuditListResponse:
    return AuditListResponse(items=[], total=0, page=page, page_size=page_size)

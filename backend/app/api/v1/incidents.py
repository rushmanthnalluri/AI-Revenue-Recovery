"""Incident endpoints. Owner: detection/incidents agent."""

from datetime import datetime

from fastapi import APIRouter, Query

from app.ports import IncidentStatus, Severity
from app.schemas.incidents import IncidentListResponse

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])


@router.get("", response_model=IncidentListResponse)
def list_incidents(
    status: IncidentStatus | None = Query(default=None),
    severity: Severity | None = Query(default=None),
    metric: str | None = Query(default=None),
    detected_from: datetime | None = Query(default=None),
    detected_to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> IncidentListResponse:
    return IncidentListResponse(items=[], total=0, page=page, page_size=page_size)


@router.get("/{incident_id}", status_code=501)
def get_incident(incident_id: str):
    # 501 stub: detail shape is app.schemas.incidents.IncidentDetail.
    from app.api import not_implemented

    return not_implemented("incident detail")

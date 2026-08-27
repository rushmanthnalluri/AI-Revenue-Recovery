"""Detection endpoints. Owner: detection agent.

Exempt from the X-API-Key requirement outside prod (see app.main) so the demo
UI can trigger detection runs directly.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.detection import (
    DetectionIncidentView,
    DetectionRunRequest,
    DetectionRunResponse,
)
from app.services.detection import run_detection

router = APIRouter(prefix="/api/v1/detection", tags=["detection"])


@router.post("/run", response_model=DetectionRunResponse)
def run_detection_endpoint(
    body: DetectionRunRequest | None = None,
    db: Session = Depends(get_db),
) -> DetectionRunResponse:
    """Execute one detection pass over payment_events and persist incidents.

    Re-running with the same window/segment/detector updates the existing
    incident instead of duplicating it; overlapping scheduled passes MERGE
    into the open episode (original ``detected_at`` preserved); incidents must
    clear the noise floors (``min_absolute_deviation`` / ``min_flagged_volume``
    / ``min_flagged_run``) or they are counted in ``anomalies_filtered`` and
    dropped. ``dry_run`` computes everything but persists nothing.
    """
    req = body or DetectionRunRequest()
    try:
        result = run_detection(db, req)
    except ValueError as exc:
        # unknown detector / metric names
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DetectionRunResponse(
        run_id=result.run_id,
        status=result.status,
        started_at=result.started_at,
        finished_at=result.finished_at,
        anomalies_detected=result.anomalies_detected,
        anomalies_filtered=result.anomalies_filtered,
        incidents_created=result.incidents_created,
        incidents_updated=result.incidents_updated,
        detail=result.detail,
        incidents=[
            DetectionIncidentView(
                incident_id=i.incident_id,
                action=i.action,
                metric=i.metric,
                detector=i.detector,
                severity=i.severity,
                baseline_value=i.baseline_value,
                observed_value=i.observed_value,
                deviation_pct=i.deviation_pct,
                segment=i.segment,
                window_start=i.window_start,
                window_end=i.window_end,
                detected_at=i.detected_at,
                anomaly_start=i.anomaly_start,
                affected_payments_count=i.affected_payments_count,
                revenue_at_risk_paise=i.revenue_at_risk_paise,
                currency=i.currency,
                detail=i.detail,
            )
            for i in result.incidents
        ],
    )

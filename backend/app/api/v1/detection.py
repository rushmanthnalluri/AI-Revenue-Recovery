"""Detection endpoints. Owner: detection agent.

Exempt from the X-API-Key requirement outside prod (see app.main) so the demo
UI can trigger detection runs directly."""

from fastapi import APIRouter

from app.api import not_implemented
from app.schemas.detection import DetectionRunRequest

router = APIRouter(prefix="/api/v1/detection", tags=["detection"])


@router.post("/run")
def run_detection(body: DetectionRunRequest | None = None):
    # 501 stub: response shape is app.schemas.detection.DetectionRunResponse.
    return not_implemented("detection run")

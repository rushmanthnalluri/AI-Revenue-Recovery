"""Detection schemas — on-demand anomaly detection runs."""

from datetime import datetime

from pydantic import BaseModel, Field


class DetectionRunRequest(BaseModel):
    window_minutes: int = Field(default=60, ge=5, le=24 * 60)
    metrics: list[str] | None = None  # default: all known metrics
    dry_run: bool = False  # detect but do not create incidents


class DetectionRunResponse(BaseModel):
    run_id: str
    status: str  # completed | failed
    started_at: datetime
    finished_at: datetime | None = None
    anomalies_detected: int = 0
    incidents_created: list[str] = Field(default_factory=list)
    detail: str | None = None

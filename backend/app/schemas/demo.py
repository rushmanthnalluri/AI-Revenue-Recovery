"""Demo schemas — judge-facing scenario triggers and environment reset."""

from datetime import datetime

from pydantic import BaseModel, Field


class DemoResetResponse(BaseModel):
    status: str = "ok"
    cleared: dict[str, int] = Field(default_factory=dict)  # table -> rows deleted
    reset_at: datetime | None = None


class ScenarioInfo(BaseModel):
    name: str
    description: str
    expected_incident_metric: str | None = None


class ScenarioListResponse(BaseModel):
    scenarios: list[ScenarioInfo] = Field(default_factory=list)


class ScenarioTriggerResponse(BaseModel):
    scenario: str
    status: str  # started | completed | failed
    simulator_run_id: str | None = None
    incident_id: str | None = None
    detail: str | None = None

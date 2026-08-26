"""Demo endpoints. Owner: demo/simulator agent.

Exempt from the X-API-Key requirement outside prod (see app.main) so judges can
reset and trigger scenarios from the UI."""

from fastapi import APIRouter

from app.api import not_implemented
from app.schemas.demo import ScenarioListResponse

router = APIRouter(prefix="/api/v1/demo", tags=["demo"])


@router.post("/reset")
def reset_demo():
    # 501 stub: response shape is app.schemas.demo.DemoResetResponse.
    return not_implemented("demo reset")


@router.post("/scenario/{name}")
def trigger_scenario(name: str):
    # 501 stub: response shape is app.schemas.demo.ScenarioTriggerResponse.
    return not_implemented(f"demo scenario '{name}'")


@router.get("/scenarios", response_model=ScenarioListResponse)
def list_scenarios() -> ScenarioListResponse:
    return ScenarioListResponse(scenarios=[])

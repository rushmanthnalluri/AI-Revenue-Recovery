"""Evaluation schemas — runs, metrics, and trigger contracts."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import Paginated


class EvaluationRunSummary(BaseModel):
    id: str
    name: str
    evaluation_type: str  # detection | diagnosis | recovery | end_to_end
    dataset: str
    simulator_run_id: str | None = None
    status: str  # running | completed | failed
    started_at: datetime | None = None
    finished_at: datetime | None = None


class EvaluationRunListResponse(Paginated[EvaluationRunSummary]):
    pass


class EvaluationRunDetail(EvaluationRunSummary):
    metrics: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class EvaluationMetrics(BaseModel):
    """Aggregate metrics over completed runs (scientific evaluation vs ground
    truth — ADR 0005). All rates are 0..1; latencies in minutes."""

    runs_count: int = 0
    detection_precision: float | None = None
    detection_recall: float | None = None
    detection_f1: float | None = None
    diagnosis_top1_accuracy: float | None = None
    mean_time_to_detect_minutes: float | None = None
    mean_time_to_recover_minutes: float | None = None
    recovery_rate: float | None = None
    recovered_revenue_paise: int = 0
    false_action_rate: float | None = None  # policy-approved actions that were wrong
    currency: str = "INR"
    # --- additive ---
    diagnosis_top3_accuracy: float | None = None
    unsafe_action_count: int = 0  # executed actions without gate evidence; must be 0
    baseline_recovery_rate: float | None = None  # naive-retry arm comparison
    baseline_recovered_revenue_paise: int = 0
    # Mean incremental lift (treatment − holdout recovery rate) over completed
    # runs that carried a randomized holdout; None when no run has one.
    incremental_lift: float | None = None


class RunEvaluationRequest(BaseModel):
    name: str = "adhoc"
    evaluation_type: str = "end_to_end"
    dataset: str = "simulator"  # simulator scenario name or "production"
    simulator_run_id: str | None = None
    # --- additive scale knobs ---
    # Scenario name from `app.simulator.SCENARIOS`; overrides `dataset`.
    scenario: str | None = None
    # None -> use the scenario preset's own scale (detection needs roughly
    # preset-scale traffic density; smaller scales are for plumbing tests).
    seed: int | None = None
    days: int | None = None
    events: int | None = None
    customers: int | None = None
    # Share of customers randomized into the no-action holdout inside the
    # PulseRecover arm. None -> harness default (0.10); 0 disables it.
    holdout_fraction: float | None = None


class RunEvaluationResponse(BaseModel):
    run_id: str
    status: str
    started_at: datetime
    # --- additive ---
    finished_at: datetime | None = None
    experiment_id: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)

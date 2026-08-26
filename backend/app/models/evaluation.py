"""Evaluation domain: experiments, model predictions, evaluation runs, the
simulator (with ground truth for scientific scoring — ADR 0005), and agent
reports."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app import ids
from app.db import Base, TZDateTime
from app.models.base import TimestampMixin


class Experiment(TimestampMixin, Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.experiment_id)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(sa.Text)
    hypothesis: Mapped[str | None] = mapped_column(sa.Text)
    config: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    # draft | running | completed | aborted
    status: Mapped[str] = mapped_column(sa.String(32), default="draft", nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    results: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)


class ModelPrediction(TimestampMixin, Base):
    """Every model inference, persisted for audit and offline evaluation."""

    __tablename__ = "model_predictions"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.prediction_id)
    incident_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="SET NULL"), index=True
    )
    model_name: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(sa.String(64), default="0", nullable=False)
    # anomaly | diagnosis | revenue_risk | recovery_propensity
    prediction_type: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    input_features: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    score: Mapped[float | None] = mapped_column(sa.Float)


class EvaluationRun(TimestampMixin, Base):
    __tablename__ = "evaluation_runs"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.evaluation_run_id)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    # detection | diagnosis | recovery | end_to_end
    evaluation_type: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    # simulator scenario name or "production"
    dataset: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    simulator_run_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("simulator_runs.id", ondelete="SET NULL"), index=True
    )
    # running | completed | failed
    status: Mapped[str] = mapped_column(sa.String(32), default="running", nullable=False, index=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    notes: Mapped[str | None] = mapped_column(sa.Text)


class SimulatorRun(TimestampMixin, Base):
    __tablename__ = "simulator_runs"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.simulator_run_id)
    scenario: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    seed: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    # running | completed | failed
    status: Mapped[str] = mapped_column(sa.String(32), default="running", nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    stats: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)

    ground_truth: Mapped[list["SimulatorGroundTruth"]] = relationship(
        back_populates="simulator_run", cascade="all, delete-orphan"
    )


class SimulatorGroundTruth(TimestampMixin, Base):
    """What the simulator actually injected — the scoring key that makes
    detection/diagnosis/recovery evaluation scientific instead of anecdotal."""

    __tablename__ = "simulator_ground_truth"
    __table_args__ = (
        sa.UniqueConstraint("simulator_run_id", "entity_type", "entity_id", name="uq_ground_truth_entity"),
    )

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.ground_truth_id)
    simulator_run_id: Mapped[str] = mapped_column(
        sa.ForeignKey("simulator_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # incident | payment | subscription
    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # Injected cause, expected incident signature, expected recovery outcome...
    truth: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)

    simulator_run: Mapped[SimulatorRun] = relationship(back_populates="ground_truth")


class AgentReport(TimestampMixin, Base):
    """Persisted output of an AI/heuristic agent run (investigation, strategy...)."""

    __tablename__ = "agent_reports"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.agent_report_id)
    incident_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="SET NULL"), index=True
    )
    # investigator | diagnostician | strategist
    agent_name: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    # investigation | diagnosis | strategy
    report_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # running | completed | failed
    status: Mapped[str] = mapped_column(sa.String(32), default="completed", nullable=False, index=True)
    input: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    # llm model name or "heuristic"
    model: Mapped[str] = mapped_column(sa.String(128), default="heuristic", nullable=False)
    tokens_used: Mapped[int | None] = mapped_column(sa.Integer)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    error: Mapped[str | None] = mapped_column(sa.Text)

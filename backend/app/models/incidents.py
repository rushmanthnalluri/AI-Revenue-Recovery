"""Incident domain: detected anomalies, collected evidence, ML diagnoses."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app import ids
from app.db import Base, TZDateTime
from app.models.base import TimestampMixin, enum_col
from app.ports import IncidentStatus, Severity


class Incident(TimestampMixin, Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.incident_id)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[IncidentStatus] = enum_col(
        IncidentStatus, "incident_status", default=IncidentStatus.OPEN, nullable=False, index=True
    )
    severity: Mapped[Severity] = enum_col(
        Severity, "incident_severity", default=Severity.MEDIUM, nullable=False, index=True
    )
    # Which metric deviated: payment_success_rate | authorization_rate |
    # capture_latency_ms | webhook_delay_ms | refund_rate | ...
    metric: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    # Detection provenance: zscore | ewma | isolation_forest | manual | simulator
    detection_method: Mapped[str] = mapped_column(sa.String(64), default="zscore", nullable=False)
    baseline_value: Mapped[float | None] = mapped_column(sa.Float)
    observed_value: Mapped[float | None] = mapped_column(sa.Float)
    deviation_pct: Mapped[float | None] = mapped_column(sa.Float)
    window_start: Mapped[datetime | None] = mapped_column(TZDateTime())
    window_end: Mapped[datetime | None] = mapped_column(TZDateTime())
    detected_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    affected_payments_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    revenue_at_risk_paise: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(8), default="INR", nullable=False)
    root_cause: Mapped[str | None] = mapped_column(sa.Text)  # filled after diagnosis
    # Set when the incident was injected by the simulator (links to ground truth).
    simulator_run_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    meta: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)

    evidence: Mapped[list["IncidentEvidence"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    diagnoses: Mapped[list["Diagnosis"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )


class IncidentEvidence(TimestampMixin, Base):
    __tablename__ = "incident_evidence"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.evidence_id)
    incident_id: Mapped[str] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # payment_sample | webhook_log | gateway_response | metric_series | customer_report
    evidence_type: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    collector: Mapped[str] = mapped_column(sa.String(64), default="agent:investigator", nullable=False)
    collected_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)

    incident: Mapped[Incident] = relationship(back_populates="evidence")


class Diagnosis(TimestampMixin, Base):
    """ML/root-cause diagnosis for an incident. Multiple versions allowed."""

    __tablename__ = "diagnoses"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.diagnosis_id)
    incident_id: Mapped[str] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    model_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(sa.String(64), default="0", nullable=False)
    # e.g. gateway_outage | bank_downtime | webhook_failure | integration_regression |
    #      insufficient_balance_pattern | otp_failure_spike
    predicted_cause: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    explanation: Mapped[str | None] = mapped_column(sa.Text)

    incident: Mapped[Incident] = relationship(back_populates="diagnoses")

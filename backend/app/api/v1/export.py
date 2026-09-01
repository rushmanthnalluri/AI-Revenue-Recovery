"""Export endpoints — CSV/JSON export for compliance and operational review.

All exports respect environment scoping (real_test vs research) and include
full provenance (source_type, source_system, external_id, ingested_at).
"""

import csv
import io
import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.models import (
    AuditLog,
    Incident,
    Payment,
    RecoveryAction,
    RecoveryOpportunity,
)
from app.models.base import (
    ENVIRONMENT_REAL_TEST,
    ENVIRONMENT_RESEARCH,
    SOURCE_TYPE_RAZORPAY_LIVE,
    SOURCE_TYPE_RAZORPAY_TEST,
    SOURCE_TYPE_SIMULATOR,
    source_types_for_environment,
)

router = APIRouter(prefix="/api/v1/export", tags=["export"])

KIND_ENVIRONMENTS = (ENVIRONMENT_REAL_TEST, ENVIRONMENT_RESEARCH)


def _env_from_param(env: str) -> str:
    if env not in KIND_ENVIRONMENTS:
        raise HTTPException(400, f"environment must be one of {KIND_ENVIRONMENTS}")
    return env


def _make_filename(prefix: str, env: str, ext: str) -> str:
    ts = utcnow().strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{env}_{ts}.{ext}"


def _csv_response(content: str, filename: str) -> StreamingResponse:
    return StreamingResponse(
        io.StringIO(content),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _json_response(content: str, filename: str) -> StreamingResponse:
    return StreamingResponse(
        io.StringIO(content),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/audit")
def export_audit(
    db: Session = Depends(get_db),
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    environment: Literal["real_test", "research"] = Query(default="real_test"),
    format: Literal["csv", "json"] = Query(default="csv"),
) -> StreamingResponse:
    """Export audit trail — append-only record of every decision and action."""
    env = _env_from_param(environment)
    filters = [AuditLog.environment == env]
    if entity_type:
        filters.append(AuditLog.entity_type == entity_type)
    if entity_id:
        filters.append(AuditLog.entity_id == entity_id)

    rows = db.scalars(
        db.query(AuditLog).where(*filters).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    ).all()

    filename = _make_filename("audit", env, format)

    if format == "json":
        data = [
            {
                "id": r.id,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "actor": r.actor,
                "action": r.action,
                "details": r.details,
                "request_id": r.request_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "environment": r.environment or "research",
            }
            for r in rows
        ]
        return _json_response(json.dumps(data, indent=2), filename)

    # CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "entity_type",
            "entity_id",
            "actor",
            "action",
            "details_json",
            "request_id",
            "created_at",
            "environment",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.id,
                r.entity_type,
                r.entity_id,
                r.actor,
                r.action,
                json.dumps(r.details or {}),
                r.request_id,
                r.created_at.isoformat() if r.created_at else "",
                r.environment or "research",
            ]
        )
    return _csv_response(output.getvalue(), filename)


@router.get("/incidents")
def export_incidents(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    metric: str | None = Query(default=None),
    detected_from: datetime | None = Query(default=None),
    detected_to: datetime | None = Query(default=None),
    environment: Literal["real_test", "research"] = Query(default="real_test"),
    format: Literal["csv", "json"] = Query(default="csv"),
) -> StreamingResponse:
    """Export incidents — detected degradations with revenue impact."""
    env = _env_from_param(environment)
    filters = [Incident.environment == env]
    if status:
        filters.append(Incident.status == status)
    if severity:
        filters.append(Incident.severity == severity)
    if metric:
        filters.append(Incident.metric == metric)
    if detected_from:
        filters.append(Incident.detected_at >= detected_from)
    if detected_to:
        filters.append(Incident.detected_at <= detected_to)

    rows = db.scalars(
        db.query(Incident).where(*filters).order_by(Incident.detected_at.desc(), Incident.id.desc())
    ).all()

    filename = _make_filename("incidents", env, format)

    if format == "json":
        data = [
            {
                "id": r.id,
                "title": r.title,
                "description": r.description,
                "status": r.status.value if r.status else None,
                "severity": r.severity.value if r.severity else None,
                "metric": r.metric,
                "detection_method": r.detection_method,
                "detected_at": r.detected_at.isoformat() if r.detected_at else None,
                "window_start": r.window_start.isoformat() if r.window_start else None,
                "window_end": r.window_end.isoformat() if r.window_end else None,
                "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
                "baseline_value": r.baseline_value,
                "observed_value": r.observed_value,
                "deviation_pct": r.deviation_pct,
                "affected_payments_count": r.affected_payments_count,
                "revenue_at_risk_paise": r.revenue_at_risk_paise,
                "currency": r.currency,
                "root_cause": r.root_cause,
                "simulator_run_id": r.simulator_run_id,
                "environment": r.environment or "research",
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return _json_response(json.dumps(data, indent=2), filename)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "title",
            "description",
            "status",
            "severity",
            "metric",
            "detection_method",
            "detected_at",
            "window_start",
            "window_end",
            "resolved_at",
            "baseline_value",
            "observed_value",
            "deviation_pct",
            "affected_payments_count",
            "revenue_at_risk_paise",
            "currency",
            "root_cause",
            "simulator_run_id",
            "environment",
            "created_at",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.id,
                r.title,
                r.description or "",
                r.status.value if r.status else "",
                r.severity.value if r.severity else "",
                r.metric or "",
                r.detection_method or "",
                r.detected_at.isoformat() if r.detected_at else "",
                r.window_start.isoformat() if r.window_start else "",
                r.window_end.isoformat() if r.window_end else "",
                r.resolved_at.isoformat() if r.resolved_at else "",
                r.baseline_value or "",
                r.observed_value or "",
                r.deviation_pct or "",
                r.affected_payments_count or "",
                r.revenue_at_risk_paise or "",
                r.currency or "INR",
                r.root_cause or "",
                r.simulator_run_id or "",
                r.environment or "research",
                r.created_at.isoformat() if r.created_at else "",
            ]
        )
    return _csv_response(output.getvalue(), filename)


@router.get("/recovery")
def export_recovery(
    db: Session = Depends(get_db),
    environment: Literal["real_test", "research"] = Query(default="real_test"),
    format: Literal["csv", "json"] = Query(default="csv"),
) -> StreamingResponse:
    """Export recovery opportunities + actions — full lifecycle from proposal to verification."""
    env = _env_from_param(environment)

    opps = db.scalars(
        db.query(RecoveryOpportunity)
        .where(RecoveryOpportunity.environment == env)
        .order_by(RecoveryOpportunity.created_at.desc(), RecoveryOpportunity.id.desc())
    ).all()

    filename = _make_filename("recovery", env, format)

    if format == "json":
        data = []
        for opp in opps:
            actions = db.scalars(
                db.query(RecoveryAction)
                .where(RecoveryAction.opportunity_id == opp.id)
                .order_by(RecoveryAction.created_at, RecoveryAction.id)
            ).all()
            data.append(
                {
                    "opportunity": {
                        "id": opp.id,
                        "incident_id": opp.incident_id,
                        "payment_id": opp.payment_id,
                        "customer_id": opp.customer_id,
                        "subscription_id": opp.subscription_id,
                        "opportunity_type": opp.opportunity_type,
                        "status": opp.status.value if opp.status else None,
                        "amount_paise": opp.amount_paise,
                        "currency": opp.currency,
                        "expected_recovery_paise": opp.expected_recovery_paise,
                        "confidence": opp.confidence,
                        "risk": opp.risk,
                        "reason": opp.reason,
                        "created_at": opp.created_at.isoformat() if opp.created_at else None,
                        "expires_at": opp.expires_at.isoformat() if opp.expires_at else None,
                        "environment": opp.environment or "research",
                    },
                    "actions": [
                        {
                            "id": a.id,
                            "action_type": a.action_type.value if a.action_type else None,
                            "status": a.status.value if a.status else None,
                            "amount_paise": a.amount_paise,
                            "currency": a.currency,
                            "confidence": a.confidence,
                            "actor": a.actor,
                            "attempts": a.attempts,
                            "gateway_request_id": a.gateway_request_id,
                            "proposed_at": a.proposed_at.isoformat() if a.proposed_at else None,
                            "executed_at": a.executed_at.isoformat() if a.executed_at else None,
                            "verified_at": a.verified_at.isoformat() if a.verified_at else None,
                            "completed_at": a.completed_at.isoformat() if a.completed_at else None,
                            "approved_by": a.approved_by,
                            "note": a.note,
                            "last_error": a.last_error,
                        }
                        for a in actions
                    ],
                }
            )
        return _json_response(json.dumps(data, indent=2), filename)

    # CSV: flatten to one row per action, with opportunity columns repeated
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "opportunity_id",
            "incident_id",
            "payment_id",
            "customer_id",
            "subscription_id",
            "opportunity_type",
            "opportunity_status",
            "opportunity_amount_paise",
            "opportunity_currency",
            "opportunity_expected_recovery_paise",
            "opportunity_confidence",
            "opportunity_risk",
            "opportunity_reason",
            "opportunity_created_at",
            "opportunity_expires_at",
            "action_id",
            "action_type",
            "action_status",
            "action_amount_paise",
            "action_currency",
            "action_confidence",
            "action_actor",
            "action_attempts",
            "action_gateway_request_id",
            "action_proposed_at",
            "action_executed_at",
            "action_verified_at",
            "action_completed_at",
            "action_approved_by",
            "action_note",
            "action_last_error",
        ]
    )
    for opp in opps:
        actions = db.scalars(
            db.query(RecoveryAction)
            .where(RecoveryAction.opportunity_id == opp.id)
            .order_by(RecoveryAction.created_at, RecoveryAction.id)
        ).all()
        if not actions:
            writer.writerow(
                [
                    opp.id,
                    opp.incident_id or "",
                    opp.payment_id or "",
                    opp.customer_id or "",
                    opp.subscription_id or "",
                    opp.opportunity_type or "",
                    opp.status.value if opp.status else "",
                    opp.amount_paise or "",
                    opp.currency or "INR",
                    opp.expected_recovery_paise or "",
                    opp.confidence or "",
                    opp.risk or "",
                    opp.reason or "",
                    opp.created_at.isoformat() if opp.created_at else "",
                    opp.expires_at.isoformat() if opp.expires_at else "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )
        for a in actions:
            writer.writerow(
                [
                    opp.id,
                    opp.incident_id or "",
                    opp.payment_id or "",
                    opp.customer_id or "",
                    opp.subscription_id or "",
                    opp.opportunity_type or "",
                    opp.status.value if opp.status else "",
                    opp.amount_paise or "",
                    opp.currency or "INR",
                    opp.expected_recovery_paise or "",
                    opp.confidence or "",
                    opp.risk or "",
                    opp.reason or "",
                    opp.created_at.isoformat() if opp.created_at else "",
                    opp.expires_at.isoformat() if opp.expires_at else "",
                    a.id,
                    a.action_type.value if a.action_type else "",
                    a.status.value if a.status else "",
                    a.amount_paise or "",
                    a.currency or "INR",
                    a.confidence or "",
                    a.actor or "",
                    a.attempts or "",
                    a.gateway_request_id or "",
                    a.proposed_at.isoformat() if a.proposed_at else "",
                    a.executed_at.isoformat() if a.executed_at else "",
                    a.verified_at.isoformat() if a.verified_at else "",
                    a.completed_at.isoformat() if a.completed_at else "",
                    a.approved_by or "",
                    a.note or "",
                    a.last_error or "",
                ]
            )
    return _csv_response(output.getvalue(), filename)


@router.get("/payments")
def export_payments(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    method: str | None = Query(default=None),
    environment: Literal["real_test", "research"] = Query(default="real_test"),
    format: Literal["csv", "json"] = Query(default="csv"),
) -> StreamingResponse:
    """Export payments — commerce records with full provenance."""
    env = _env_from_param(environment)
    source_types = source_types_for_environment(env)

    filters = [Payment.source_type.in_(source_types)]
    if status:
        filters.append(Payment.status == status)
    if method:
        filters.append(Payment.method == method)

    rows = db.scalars(
        db.query(Payment).where(*filters).order_by(Payment.created_at.desc(), Payment.id.desc())
    ).all()

    filename = _make_filename("payments", env, format)

    if format == "json":
        data = [
            {
                "id": r.id,
                "merchant_id": r.merchant_id,
                "order_id": r.order_id,
                "customer_id": r.customer_id,
                "gateway_payment_id": r.gateway_payment_id,
                "external_id": r.external_id,
                "amount_paise": r.amount_paise,
                "currency": r.currency,
                "method": r.method,
                "status": r.status,
                "error_code": r.error_code,
                "error_description": r.error_description,
                "error_source": r.error_source,
                "captured": r.captured,
                "attempts": r.attempts,
                "gateway_created_at": r.gateway_created_at.isoformat() if r.gateway_created_at else None,
                "source_type": r.source_type,
                "source_system": r.source_system,
                "ingested_at": r.ingested_at.isoformat() if r.ingested_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return _json_response(json.dumps(data, indent=2), filename)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "merchant_id",
            "order_id",
            "customer_id",
            "gateway_payment_id",
            "external_id",
            "amount_paise",
            "currency",
            "method",
            "status",
            "error_code",
            "error_description",
            "error_source",
            "captured",
            "attempts",
            "gateway_created_at",
            "source_type",
            "source_system",
            "ingested_at",
            "created_at",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.id,
                r.merchant_id or "",
                r.order_id or "",
                r.customer_id or "",
                r.gateway_payment_id or "",
                r.external_id or "",
                r.amount_paise or "",
                r.currency or "INR",
                r.method or "",
                r.status or "",
                r.error_code or "",
                r.error_description or "",
                r.error_source or "",
                "true" if r.captured else "false",
                r.attempts or "",
                r.gateway_created_at.isoformat() if r.gateway_created_at else "",
                r.source_type or "",
                r.source_system or "",
                r.ingested_at.isoformat() if r.ingested_at else "",
                r.created_at.isoformat() if r.created_at else "",
            ]
        )
    return _csv_response(output.getvalue(), filename)


@router.get("/summary")
def export_summary(
    db: Session = Depends(get_db),
    environment: Literal["real_test", "research"] = Query(default="real_test"),
    format: Literal["csv", "json"] = Query(default="json"),
) -> StreamingResponse:
    """Export a consolidated summary — counts, revenue, recovery rates by environment."""
    env = _env_from_param(environment)
    source_types = source_types_for_environment(env)

    # Aggregate counts
    from sqlalchemy import func

    payments_total = int(db.scalar(db.query(func.count(Payment.id)).filter(Payment.source_type.in_(source_types))) or 0)
    payments_captured = int(db.scalar(db.query(func.count(Payment.id)).filter(Payment.source_type.in_(source_types), Payment.status == "captured")) or 0)
    payments_failed = int(db.scalar(db.query(func.count(Payment.id)).filter(Payment.source_type.in_(source_types), Payment.status == "failed")) or 0)

    incidents_total = int(db.scalar(db.query(func.count(Incident.id)).filter(Incident.environment == env)) or 0)
    incidents_open = int(db.scalar(db.query(func.count(Incident.id)).filter(Incident.environment == env, Incident.status == "OPEN")) or 0)

    opps_total = int(db.scalar(db.query(func.count(RecoveryOpportunity.id)).filter(RecoveryOpportunity.environment == env)) or 0)
    actions_total = int(db.scalar(db.query(func.count(RecoveryAction.id)).filter(RecoveryAction.environment == env)) or 0)
    actions_recovered = int(db.scalar(db.query(func.count(RecoveryAction.id)).filter(RecoveryAction.environment == env, RecoveryAction.status == "RECOVERED")) or 0)

    audit_total = int(db.scalar(db.query(func.count(AuditLog.id)).filter(AuditLog.environment == env)) or 0)

    revenue_captured = int(db.scalar(db.query(func.sum(Payment.amount_paise)).filter(Payment.source_type.in_(source_types), Payment.status == "captured")) or 0)
    revenue_recovered = int(db.scalar(db.query(func.sum(RecoveryAction.amount_paise)).filter(RecoveryAction.environment == env, RecoveryAction.status == "RECOVERED")) or 0)

    summary = {
        "environment": env,
        "generated_at": utcnow().isoformat().replace("+00:00", "Z"),
        "payments": {
            "total": payments_total,
            "captured": payments_captured,
            "failed": payments_failed,
            "success_rate": round(payments_captured / payments_total, 6) if payments_total else 0,
            "revenue_captured_paise": revenue_captured,
        },
        "incidents": {
            "total": incidents_total,
            "open": incidents_open,
        },
        "recovery": {
            "opportunities_total": opps_total,
            "actions_total": actions_total,
            "actions_recovered": actions_recovered,
            "recovery_rate": round(actions_recovered / actions_total, 6) if actions_total else 0,
            "revenue_recovered_paise": revenue_recovered,
        },
        "audit": {
            "total_entries": audit_total,
        },
    }

    filename = _make_filename("summary", env, format)

    if format == "json":
        return _json_response(json.dumps(summary, indent=2), filename)

    # CSV: flatten
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["metric", "value"])
    for k, v in summary["payments"].items():
        writer.writerow([f"payments.{k}", v])
    for k, v in summary["incidents"].items():
        writer.writerow([f"incidents.{k}", v])
    for k, v in summary["recovery"].items():
        writer.writerow([f"recovery.{k}", v])
    for k, v in summary["audit"].items():
        writer.writerow([f"audit.{k}", v])
    return _csv_response(output.getvalue(), filename)


__all__ = ["router"]
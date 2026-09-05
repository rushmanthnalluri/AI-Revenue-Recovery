"""AgentService — investigation orchestration.

Flow (per POST /api/v1/incidents/{id}/investigate):
1. Load the incident (404 otherwise).
2. Unless force_refresh, return the latest completed report when one exists.
3. Ensure a diagnosis exists (run DiagnosisService if missing); a diagnosis
   failure never aborts the investigation — it becomes an uncertainty.
4. Build the EvidenceBundle, select the reasoner (heuristic by default; LLM
   only when LLM_PROVIDER=openai and OPENAI_API_KEY are set), and investigate.
5. Persist the run in agent_reports (input, output, model, tokens, duration)
   and append an audit_logs row. One commit at the end.

The reasoner's output is advisory; nothing on this path executes financial
actions (the request_* tools only create PROPOSED rows gated by policy).
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.logging import get_logger
from app.models import AgentReport, Diagnosis, Incident
from app.ports import EvidenceBundle
from app.services.agent.reasoners import choose_reasoner
from app.services.agent.tools import AGENT_ACTOR, AgentTools
from app.services.diagnosis.service import DiagnosisError, DiagnosisService
from app.services.policy import audit

log = get_logger(__name__)

INVESTIGATOR_AGENT = "investigator"
INVESTIGATION_REPORT_TYPE = "investigation"


class AgentError(RuntimeError):
    """An investigation could not be produced."""


class IncidentNotFoundError(AgentError):
    """The requested incident does not exist."""


class AgentService:
    def __init__(
        self,
        session: Session,
        *,
        artifacts_dir: str | os.PathLike[str] | None = None,
        reasoner_factory: Callable[[AgentTools], Any] | None = None,
    ) -> None:
        self._session = session
        self._artifacts_dir = artifacts_dir
        self._reasoner_factory = reasoner_factory

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def investigate(self, incident_id: str, *, force_refresh: bool = False) -> AgentReport:
        incident = self._session.get(Incident, incident_id)
        if incident is None:
            raise IncidentNotFoundError(f"incident not found: {incident_id!r}")

        if not force_refresh:
            existing = self.latest(incident_id)
            if existing is not None:
                return existing

        tools = AgentTools(self._session, incident_id=incident_id)
        diagnosis, diagnosis_error = self._ensure_diagnosis(incident)
        diagnosis_ctx = self._diagnosis_context(tools) if diagnosis else None

        bundle = EvidenceBundle(
            incident_id=incident.id,
            metric=incident.metric,
            window_start=incident.window_start,
            window_end=incident.window_end,
            events=[],
            context={"diagnosis": diagnosis_ctx, "diagnosis_error": diagnosis_error},
        )

        reasoner = (
            self._reasoner_factory(tools)
            if self._reasoner_factory is not None
            else self._default_reasoner(tools)
        )

        started = time.perf_counter()
        try:
            report = reasoner.investigate(bundle)
        except Exception as exc:  # persist the failure for auditability, then surface it
            duration_ms = int((time.perf_counter() - started) * 1000)
            row = AgentReport(
                incident_id=incident_id,
                agent_name=INVESTIGATOR_AGENT,
                report_type=INVESTIGATION_REPORT_TYPE,
                status="failed",
                input=self._input_payload(incident, force_refresh, diagnosis_ctx),
                output={},
                model=type(reasoner).__name__,
                duration_ms=duration_ms,
                error=f"{type(exc).__name__}: {exc}",
            )
            self._session.add(row)
            self._session.flush()
            audit.record(
                self._session,
                actor=AGENT_ACTOR,
                action="agent.investigate_failed",
                entity_type="agent_report",
                entity_id=row.id,
                details={"incident_id": incident_id, "error": row.error},
            )
            self._session.commit()
            raise AgentError(f"investigation failed for incident {incident_id!r}: {exc}") from exc
        duration_ms = int((time.perf_counter() - started) * 1000)

        structured = report.raw.get("structured", {})
        row = AgentReport(
            incident_id=incident_id,
            agent_name=INVESTIGATOR_AGENT,
            report_type=INVESTIGATION_REPORT_TYPE,
            status="completed",
            input=self._input_payload(incident, force_refresh, diagnosis_ctx),
            output=structured,
            model=str(report.generated_by)[:128],
            tokens_used=report.raw.get("tokens_used"),
            duration_ms=duration_ms,
        )
        self._session.add(row)
        self._session.flush()
        audit.record(
            self._session,
            actor=AGENT_ACTOR,
            action="agent.investigate",
            entity_type="agent_report",
            entity_id=row.id,
            details={
                "incident_id": incident_id,
                "reasoner": structured.get("reasoner"),
                "model": report.generated_by,
                "confidence": structured.get("confidence"),
                "escalated": structured.get("escalated"),
                "degraded": structured.get("degraded"),
                "stripped_claims": len(structured.get("stripped_claims") or []),
                "tools_called": structured.get("tools_called"),
                "llm_fallback": bool(report.raw.get("llm_fallback")),
            },
        )
        self._session.commit()
        log.info(
            "investigation completed",
            extra={
                "incident_id": incident_id,
                "report_id": row.id,
                "model": report.generated_by,
                "confidence": structured.get("confidence"),
                "escalated": structured.get("escalated"),
                "degraded": structured.get("degraded"),
                "duration_ms": duration_ms,
            },
        )
        return row

    def latest(self, incident_id: str) -> AgentReport | None:
        """Most recent completed investigation report for an incident."""
        return self._session.scalars(
            sa.select(AgentReport)
            .where(
                AgentReport.incident_id == incident_id,
                AgentReport.agent_name == INVESTIGATOR_AGENT,
                AgentReport.report_type == INVESTIGATION_REPORT_TYPE,
                AgentReport.status == "completed",
            )
            .order_by(AgentReport.created_at.desc())
            .limit(1)
        ).first()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _ensure_diagnosis(self, incident: Incident) -> tuple[bool, str | None]:
        """Run DiagnosisService when no diagnosis exists yet.

        Returns (has_diagnosis, error). A diagnosis failure is recorded as an
        uncertainty, never raised: the investigation proceeds on tool data.
        """
        existing = self._session.scalar(
            sa.select(sa.func.count())
            .select_from(Diagnosis)
            .where(Diagnosis.incident_id == incident.id)
        )
        if existing:
            return True, None
        try:
            DiagnosisService(self._session, self._artifacts_dir).classify(incident.id)
            return True, None
        except DiagnosisError as exc:
            log.warning(
                "diagnosis failed during investigation",
                extra={"incident_id": incident.id, "error": str(exc)},
            )
            return False, str(exc)

    @staticmethod
    def _diagnosis_context(tools: AgentTools) -> dict[str, Any] | None:
        # Direct method call (not tools.call): context assembly is not a
        # reasoner-visible tool invocation, so it stays out of the call log.
        return tools.get_incident().data.get("diagnosis")

    @staticmethod
    def _default_reasoner(tools: AgentTools):
        from app.config import settings

        return choose_reasoner(
            tools,
            llm_provider=settings.LLM_PROVIDER,
            openai_api_key=settings.OPENAI_API_KEY,
            openai_base_url=settings.OPENAI_BASE_URL,
            openai_model=settings.OPENAI_MODEL,
            pollinations_api_key=settings.POLLINATIONS_API_KEY,
            pollinations_base_url=settings.POLLINATIONS_BASE_URL,
            pollinations_model=settings.POLLINATIONS_MODEL,
        )

    @staticmethod
    def _input_payload(
        incident: Incident,
        force_refresh: bool,
        diagnosis_ctx: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "incident_id": incident.id,
            "metric": incident.metric,
            "window_start": incident.window_start.isoformat() if incident.window_start else None,
            "window_end": incident.window_end.isoformat() if incident.window_end else None,
            "force_refresh": force_refresh,
            "diagnosis_id": (diagnosis_ctx or {}).get("diagnosis_id"),
            "assembled_at": utcnow().isoformat(),
        }


__all__ = ["AgentError", "AgentService", "IncidentNotFoundError"]

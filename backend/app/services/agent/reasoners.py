"""Reasoners — the probabilistic investigation layer. ADVISORY ONLY (ADR 0004).

Two implementations of ``ports.ReasonerProto``:

- ``HeuristicReasoner`` — DEFAULT. Offline, deterministic, no network. Builds
  the structured InvestigationOutput directly from tool results; same DB state
  always yields the same report.
- ``LlmReasoner`` — OPTIONAL. Enabled only when ``LLM_PROVIDER=openai`` or
    ``LLM_PROVIDER=pollinations`` and the matching API key is set. Runs a bounded
    tool-calling loop against an OpenAI-compatible chat endpoint, then strictly validates the output
  (JSON schema + hallucination guard). Any validation failure after a retry
  falls back to the heuristic reasoner and marks the report degraded.

Neither reasoner ever sees secrets, raw DB access, or the payment gateway.
Every financial number in a report originates from the tool layer.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Callable

from app.logging import get_logger
from app.ports import ActionType, EvidenceBundle, Hypothesis, InvestigationReport
from app.services.agent.report import (
    AUTO_EXECUTE_CONFIDENCE_FLOOR,
    ESCALATION_CONFIDENCE_THRESHOLD,
    HEURISTIC_REASONER_VERSION,
    LLM_REASONER_VERSION,
    NON_AUTO_CONFIDENCE_CAP,
    RANKED_CANDIDATE_LIMIT,
    THIN_EVIDENCE_WINDOW_FLOOR,
    AiInference,
    AlternativeHypothesis,
    InvestigationOutput,
    ObservedFact,
    PolicyOutcomeView,
    RecommendedAction,
    RevenueImplications,
)
from app.services.agent.tools import AgentTools, ToolError, ToolNotAllowed, ToolResult
from app.services.agent.validation import (
    collect_numbers,
    collect_target_ids,
    extract_json,
    validate_llm_payload,
)
from app.services.diagnosis.taxonomy import AUTO_RECOVERABLE_CAUSES

log = get_logger(__name__)

#: Diagnosis causes grouped by who likely owns the fault.
_INFRA_CAUSES = {"gateway_degradation", "route_latency", "method_outage", "bank_downtime"}
_CUSTOMER_CAUSES = {
    "abandonment_spike",
    "subscription_failure_spike",
    "customer_insufficient_funds_wave",
}

#: Failure class -> preferred first action. Transient classes get a retry,
#: intent/funds classes get a payment link, permanent ones a notification.
_ACTION_BY_FAILURE_CLASS = {
    "timeout": ActionType.RETRY_PAYMENT,
    "soft_decline": ActionType.RETRY_PAYMENT,
    "insufficient_funds": ActionType.CREATE_PAYMENT_LINK,
    "abandonment": ActionType.CREATE_PAYMENT_LINK,
    "hard_decline": ActionType.NOTIFY_CUSTOMER,
    "unknown": ActionType.NOTIFY_CUSTOMER,
}

_RATIONALE_BY_ACTION = {
    ActionType.RETRY_PAYMENT: "transient failure class — a bounded retry of the same instrument has the highest effectiveness prior",
    ActionType.CREATE_PAYMENT_LINK: "customer-intent/funds failure class — a fresh payment link lets the customer complete when ready",
    ActionType.NOTIFY_CUSTOMER: "permanent/unknown failure class — the instrument itself must be updated; a retry would waste attempt budget",
    ActionType.ESCALATE_HUMAN: "evidence is insufficient or confidence is below the escalation threshold — a human should review before any automation",
    ActionType.NO_ACTION: "diagnosis indicates no actionable fault — the anomaly is consistent with noise, so no recovery action is warranted; monitor only",
}


def _wrap_port_report(output: InvestigationOutput, *, tokens_used: int | None) -> InvestigationReport:
    """Adapt the structured report to ports.InvestigationReport (ReasonerProto)."""
    return InvestigationReport(
        incident_id=output.incident_id,
        summary=output.what_happened,
        hypotheses=[
            Hypothesis(
                title=i.statement,
                confidence=i.confidence,
                supporting_evidence=list(i.supporting_fact_ids),
            )
            for i in output.ai_inferences
        ],
        recommended_actions=[a.action_type for a in output.recommended_actions],
        generated_by=output.generated_by,
        raw={"structured": output.model_dump(mode="json"), "tokens_used": tokens_used},
    )


def _revenue_implications(revenue: ToolResult) -> RevenueImplications:
    d = revenue.data
    loss = d["observed_loss"]
    rec = d["recoverable"]
    return RevenueImplications(
        currency=d.get("currency", "INR"),
        observed_loss_point_paise=loss["point_paise"],
        observed_loss_lower_paise=loss["lower_paise"],
        observed_loss_upper_paise=loss["upper_paise"],
        recoverable_point_paise=rec["point_paise"],
        recoverable_lower_paise=rec["lower_paise"],
        recoverable_upper_paise=rec["upper_paise"],
        expected_recovery_point_by_strategy={
            k: v["point_paise"] for k, v in d.get("expected_recovery_by_strategy", {}).items()
        },
        actual_recovered_paise=d.get("actual_recovered_paise", 0),
        recovered_actions_count=d.get("recovered_actions_count", 0),
        confidence=loss.get("confidence", 0.0),
        low_confidence=bool(loss.get("low_confidence", True)),
        basis=loss.get("basis", ""),
    )


def _evidence_confidence(
    diagnosis: dict[str, Any] | None,
    stats: dict[str, Any],
    revenue: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> float:
    """Deterministic evidence-calibrated confidence: diagnosis confidence
    adjusted by evidence sufficiency. No randomness, no wall-clock. Shared by
    both reasoners — the LLM path caps the model's self-reported confidence at
    this ceiling (a model may be less confident than the evidence, never more,
    once it approaches the auto-execute floor)."""
    conf = float(diagnosis["confidence"]) if diagnosis else 0.30
    failed = stats["window"]["failed"]
    if failed >= 10:
        conf += 0.10
    elif failed == 0:
        conf -= 0.20
    if revenue["observed_loss"]["low_confidence"]:
        conf -= 0.10
    if not candidates:
        conf -= 0.10
    return round(min(0.95, max(0.05, conf)), 4)


def _gate_confidence(confidence: float, diagnosis: dict[str, Any] | None) -> float:
    """Confidence passed to the policy preview. For diagnosis classes outside
    AUTO_RECOVERABLE_CAUSES the agent must never preview an auto-execute lane
    (the ML track measured the diagnosis artifact crossing the 0.85 floor on
    52.8% of non-auto-recoverable production frames), so the gate input is
    capped strictly below the floor."""
    label = (diagnosis or {}).get("label")
    if diagnosis and label not in AUTO_RECOVERABLE_CAUSES and label != "no_fault":
        return min(confidence, NON_AUTO_CONFIDENCE_CAP)
    return confidence


def _action_from_policy_preview(
    preview: ToolResult,
    *,
    rationale: str,
    expected_recovery_paise: int | None,
) -> RecommendedAction:
    d = preview.data
    outcome = d["policy"]["outcome"]
    reasons = d["policy"]["reasons"]
    # The rationale must never be a bare "retry it": the live gate outcome is
    # attached by the system, in text, right next to the proposal.
    policy_note = f" Policy preview: {outcome}"
    if reasons:
        policy_note += f" — {reasons[0]}"
    policy_note += "."
    if outcome not in rationale:
        rationale = rationale.rstrip() + policy_note
    return RecommendedAction(
        action_type=d["action_type"],
        rationale=rationale,
        amount_paise=d.get("amount_paise"),
        currency=d.get("currency", "INR"),
        payment_id=d.get("payment_id"),
        opportunity_id=d.get("opportunity_id"),
        expected_recovery_paise=expected_recovery_paise,
        confidence=d.get("confidence"),
        policy_preview=PolicyOutcomeView(**d["policy"]),
    )


class HeuristicReasoner:
    """Default reasoner: deterministic rules over tool evidence. No network."""

    def __init__(
        self,
        tools: AgentTools,
        *,
        escalation_threshold: float = ESCALATION_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._tools = tools
        self._threshold = escalation_threshold

    # -- ReasonerProto --------------------------------------------------------

    def investigate(self, evidence: EvidenceBundle) -> InvestigationReport:
        start_idx = len(self._tools.calls)
        diagnosis = (evidence.context or {}).get("diagnosis")
        diagnosis_error = (evidence.context or {}).get("diagnosis_error")

        inc = self._tools.call("get_incident")
        stats = self._tools.call("get_payment_stats")
        dist = self._tools.call("get_failure_distribution")
        revenue = self._tools.call("get_revenue_at_risk")
        candidates = self._tools.call("get_recovery_candidates")

        facts: list[ObservedFact] = []

        def add_fact(statement: str, result: ToolResult, data: dict[str, Any]) -> str:
            fid = f"f{len(facts) + 1}"
            facts.append(
                ObservedFact(
                    id=fid,
                    statement=statement,
                    tool=result.name,
                    evidence_ids=list(result.evidence_ids),
                    data=data,
                )
            )
            return fid

        inc_d = inc.data
        f_incident = add_fact(
            (
                f"Incident {inc_d['incident_id']} ({inc_d['severity']}) on metric "
                f"'{inc_d['metric']}': observed {inc_d['observed_value']} vs baseline "
                f"{inc_d['baseline_value']} (deviation {inc_d['deviation_pct']}%, "
                f"detector {inc_d['detection_method']})."
            ),
            inc,
            {
                "metric": inc_d["metric"],
                "severity": inc_d["severity"],
                "observed_value": inc_d["observed_value"],
                "baseline_value": inc_d["baseline_value"],
                "deviation_pct": inc_d["deviation_pct"],
            },
        )

        w, b = stats.data["window"], stats.data["baseline"]
        f_stats = add_fact(
            (
                f"Incident window: {w['failed']} of {w['total']} payments failed "
                f"(failure_rate {w['failure_rate']}) versus baseline "
                f"{b['failed']} of {b['total']} (failure_rate {b['failure_rate']}); "
                f"failed amount in window {w['failed_amount_paise']} paise."
            ),
            stats,
            {
                "window_failed": w["failed"],
                "window_total": w["total"],
                "window_failure_rate": w["failure_rate"],
                "baseline_failure_rate": b["failure_rate"],
                "failure_rate_delta": stats.data["failure_rate_delta"],
                "window_failed_amount_paise": w["failed_amount_paise"],
            },
        )

        dist_d = dist.data
        top_reason = next(iter(dist_d["by_error_reason"]), None)
        f_dist = add_fact(
            (
                f"Failure distribution over {dist_d['failed_count']} failed payments: "
                f"by reason {dist_d['by_error_reason']}; by method {dist_d['by_method']}; "
                f"dominant failure class '{dist_d['dominant_failure_class']}'."
            ),
            dist,
            {
                "failed_count": dist_d["failed_count"],
                "by_error_reason": dist_d["by_error_reason"],
                "by_method": dist_d["by_method"],
                "by_failure_class": dist_d["by_failure_class"],
                "dominant_failure_class": dist_d["dominant_failure_class"],
                "top_error_reason": top_reason,
            },
        )

        rev_d = revenue.data
        f_revenue = add_fact(
            (
                f"Counterfactual observed loss {rev_d['observed_loss']['point_paise']} paise "
                f"(band {rev_d['observed_loss']['lower_paise']}.."
                f"{rev_d['observed_loss']['upper_paise']}); recoverable "
                f"{rev_d['recoverable']['point_paise']} paise; verified recovered so far "
                f"{rev_d['actual_recovered_paise']} paise."
            ),
            revenue,
            {
                "observed_loss_point_paise": rev_d["observed_loss"]["point_paise"],
                "observed_loss_lower_paise": rev_d["observed_loss"]["lower_paise"],
                "observed_loss_upper_paise": rev_d["observed_loss"]["upper_paise"],
                "recoverable_point_paise": rev_d["recoverable"]["point_paise"],
                "actual_recovered_paise": rev_d["actual_recovered_paise"],
            },
        )

        cand_list = candidates.data["candidates"]
        f_candidates: str | None = None
        if cand_list:
            amounts = [c["amount_paise"] for c in cand_list]
            f_candidates = add_fact(
                (
                    f"{len(cand_list)} recovery candidate(s) identified "
                    f"(source {candidates.data['source']}), largest exposure "
                    f"{max(amounts)} paise, total {sum(amounts)} paise."
                ),
                candidates,
                {
                    "candidate_count": len(cand_list),
                    "largest_amount_paise": max(amounts),
                    "total_amount_paise": sum(amounts),
                    "source": candidates.data["source"],
                },
            )

        f_customer: str | None = None
        # Check the opt-out flag of the customers behind the top candidates
        # BEFORE targets are chosen: recommending an action on an opted-out
        # customer headlines a recommendation the gate hard-blocks
        # (never_auto_execute.customer_opted_out). Lazy: stop once
        # RANKED_CANDIDATE_LIMIT eligible candidates are ranked; bounded at 3
        # customer-history reads total.
        customer_opted_out: dict[str, bool] = {}
        eligible_candidates: list[dict[str, Any]] = []
        for cand in sorted(cand_list, key=lambda c: c["amount_paise"], reverse=True):
            if len(eligible_candidates) >= RANKED_CANDIDATE_LIMIT:
                break
            cid = cand.get("customer_id")
            if not cid:
                # no customer to check — eligible as far as we can tell
                eligible_candidates.append(cand)
                continue
            if cid in customer_opted_out:
                if not customer_opted_out[cid]:
                    eligible_candidates.append(cand)
                continue
            if len(customer_opted_out) >= 3:
                break
            history = self._tools.call("get_customer_history", {"customer_id": cid})
            hd = history.data
            customer_opted_out[cid] = bool(hd["opted_out"])
            f_customer = add_fact(
                (
                    f"Customer {hd['customer_id']}: {hd['total_payments']} payments, "
                    f"{hd['captured_count']} captured, {hd['failed_count']} failed; "
                    f"opted_out={hd['opted_out']}."
                ),
                history,
                {
                    "customer_id": hd["customer_id"],
                    "total_payments": hd["total_payments"],
                    "captured_count": hd["captured_count"],
                    "failed_count": hd["failed_count"],
                    "opted_out": hd["opted_out"],
                },
            )
            if not customer_opted_out[cid]:
                eligible_candidates.append(cand)

        # -- inferences -------------------------------------------------------
        inferences: list[AiInference] = []
        uncertainties: list[str] = []
        alternatives: list[AlternativeHypothesis] = []

        if diagnosis:
            diag_support = [f_incident, f_stats, f_dist]
            inferences.append(
                AiInference(
                    id="i1",
                    statement=(
                        f"Root cause is most likely '{diagnosis['label']}' "
                        f"(model {diagnosis['model_name']}@{diagnosis['model_version']})."
                    ),
                    label="root_cause",
                    confidence=round(float(diagnosis["confidence"]), 4),
                    supporting_fact_ids=diag_support,
                )
            )
            if diagnosis.get("heuristic"):
                uncertainties.append(
                    "Diagnosis came from the rule-based fallback (no trained model artifact); "
                    "treat its confidence as advisory."
                )
            label = diagnosis["label"]
            if label in _INFRA_CAUSES:
                nature = "infrastructure"
                nature_stmt = "The failure signature is infrastructure-side (gateway/bank/route), not customer intent — recovery actions have high effectiveness priors."
            elif label in _CUSTOMER_CAUSES:
                nature = "customer_intent"
                nature_stmt = "The failure signature is customer-intent-side (abandonment/funds), so recovery depends on re-engaging the customer, not retrying rails."
            else:
                nature = "no_clear_fault"
                nature_stmt = "The diagnosis does not indicate a clear fault; the anomaly may be noise."
            inferences.append(
                AiInference(
                    id="i2",
                    statement=nature_stmt,
                    label="failure_nature",
                    confidence=round(float(diagnosis["confidence"]) * 0.9, 4),
                    supporting_fact_ids=[f_dist],
                )
            )
            for rank, entry in enumerate((diagnosis.get("top3") or [])[1:4], start=1):
                alternatives.append(
                    AlternativeHypothesis(
                        rank=rank,
                        cause=str(entry.get("label")),
                        confidence=round(float(entry.get("probability", 0.0)), 4),
                        source="diagnosis",
                    )
                )
        else:
            uncertainties.append("No diagnosis available for this incident.")
            if diagnosis_error:
                uncertainties.append(f"Diagnosis could not be produced: {diagnosis_error}")

        if rev_d["observed_loss"]["low_confidence"]:
            uncertainties.append(
                "Revenue-at-risk estimate is low confidence "
                f"({rev_d['observed_loss']['basis']})."
            )
        if w["total"] == 0:
            uncertainties.append("No payments observed in the incident window; evidence is thin.")
        elif w["total"] < THIN_EVIDENCE_WINDOW_FLOOR:
            uncertainties.append(
                f"Only {w['total']} payment(s) observed in the incident window; "
                "the evidence window is thin — treat conclusions as tentative."
            )
        if rev_d["observed_loss"]["point_paise"] is not None:
            inferences.append(
                AiInference(
                    id=f"i{len(inferences) + 1}",
                    statement=(
                        f"An estimated {rev_d['recoverable']['point_paise']} paise of the "
                        f"{rev_d['observed_loss']['point_paise']} paise observed loss is "
                        "recoverable under documented recoverability priors."
                    ),
                    label="recoverability",
                    confidence=round(float(rev_d["observed_loss"]["confidence"]), 4),
                    supporting_fact_ids=[f_revenue],
                )
            )

        # -- recommendation -----------------------------------------------------
        confidence = self._confidence(diagnosis, stats.data, rev_d, cand_list)
        escalated, escalation_reasons = self._escalation(
            confidence, diagnosis=diagnosis, stats=stats.data, candidates=cand_list
        )
        label = (diagnosis or {}).get("label")
        no_fault = label == "no_fault"

        if diagnosis is not None and float(diagnosis["confidence"]) < AUTO_EXECUTE_CONFIDENCE_FLOOR:
            uncertainties.append(
                f"Diagnosis confidence {round(float(diagnosis['confidence']), 4)} is below the "
                f"{AUTO_EXECUTE_CONFIDENCE_FLOOR} auto-execute floor — any automation takes the "
                "human-approval lane."
            )
        gate_confidence = _gate_confidence(confidence, diagnosis)
        if gate_confidence < confidence:
            uncertainties.append(
                f"Diagnosis class '{label}' is not auto-recoverable; a recovery proposal here "
                f"requires human approval regardless of model confidence (gate confidence capped "
                f"at {gate_confidence})."
            )
        if no_fault:
            uncertainties.append(
                "Detection fired but the diagnosis reports no actionable fault; "
                "treat the anomaly as noise unless a human disagrees."
            )
        skipped_opted_out = sum(
            1 for c in cand_list if customer_opted_out.get(c.get("customer_id") or "")
        )
        if skipped_opted_out:
            uncertainties.append(
                f"Skipped {skipped_opted_out} recovery candidate(s) whose customers opted out of contact."
            )

        def _preview(action_type: ActionType, rationale: str, conf: float,
                     target: dict[str, Any] | None = None,
                     *, per_target_expected: bool = True) -> RecommendedAction:
            args: dict[str, Any] = {"action_type": action_type.value, "confidence": conf}
            if target is not None:
                args["payment_id"] = target.get("payment_id")
                args["opportunity_id"] = target.get("opportunity_id")
            preview = self._tools.call("propose_recovery_strategy", args)
            expected = None
            eff = rev_d.get("expected_recovery_by_strategy", {}).get(action_type.value)
            if eff and target is not None and per_target_expected:
                expected = eff["point_paise"]
            return _action_from_policy_preview(
                preview, rationale=rationale, expected_recovery_paise=expected
            )

        def _escalate_action() -> RecommendedAction:
            return _preview(
                ActionType.ESCALATE_HUMAN,
                _RATIONALE_BY_ACTION[ActionType.ESCALATE_HUMAN],
                confidence,
            )

        actions: list[RecommendedAction] = []
        alternates: list[RecommendedAction] = []
        if escalated:
            actions.append(_escalate_action())
        elif no_fault:
            actions.append(
                _preview(ActionType.NO_ACTION, _RATIONALE_BY_ACTION[ActionType.NO_ACTION], confidence)
            )
        else:
            if not eligible_candidates:
                escalated = True
                escalation_reasons.append(
                    "the top recovery candidates belong to opted-out customers"
                )
                actions.append(_escalate_action())
            else:
                dominant = dist_d.get("dominant_failure_class") or "unknown"
                action_type = _ACTION_BY_FAILURE_CLASS.get(dominant, ActionType.NOTIFY_CUSTOMER)
                actions.append(
                    _preview(action_type, _RATIONALE_BY_ACTION[action_type], gate_confidence, eligible_candidates[0])
                )
                # Ranked alternates: the next-largest eligible candidates, each
                # dry-run through the same policy gate. The revenue engine
                # prices strategies incident-wide, so alternates carry no
                # per-candidate expected recovery (never an invented split).
                for rank, alt in enumerate(eligible_candidates[1:], start=2):
                    alternates.append(
                        _preview(
                            action_type,
                            (
                                f"rank {rank} alternate candidate — "
                                f"{_RATIONALE_BY_ACTION[action_type]}; "
                                f"target is the #{rank} largest eligible failed "
                                f"payment ({alt['amount_paise']} paise)"
                            ),
                            gate_confidence,
                            alt,
                            per_target_expected=False,
                        ).model_copy(update={"rank": rank})
                    )

        # Ranked candidate list: rank 1 is always the headline proposal
        # (identical to recommended_next_step), followed by the alternates.
        ranked_candidates: list[RecommendedAction] = (
            [actions[0].model_copy(update={"rank": 1}), *alternates] if actions else []
        )

        what_happened = (
            f"Detection fired on '{inc_d['metric']}' ({inc_d['severity']}): "
            f"{w['failed']} of {w['total']} payments failed in the incident window "
            f"(failure_rate {w['failure_rate']} vs baseline {b['failure_rate']}). "
            + (
                f"Diagnosis points to '{diagnosis['label']}' "
                f"(confidence {round(float(diagnosis['confidence']), 4)}). "
                if diagnosis
                else "No diagnosis is available. "
            )
            + (
                f"Counterfactual observed loss is {rev_d['observed_loss']['point_paise']} paise, "
                f"of which {rev_d['recoverable']['point_paise']} paise is estimated recoverable."
            )
        )

        tools_called = [c.name for c in self._tools.calls[start_idx:]]
        output = InvestigationOutput(
            incident_id=inc_d["incident_id"],
            what_happened=what_happened,
            observed_facts=facts,
            ai_inferences=inferences,
            alternative_hypotheses=alternatives,
            revenue_implications=_revenue_implications(revenue),
            recommended_actions=actions,
            recommended_next_step=actions[0] if actions else None,
            recommended_candidates=ranked_candidates,
            uncertainties=uncertainties,
            confidence=confidence,
            escalated=escalated,
            escalation_reasons=escalation_reasons,
            tools_called=tools_called,
            reasoner="heuristic",
            generated_by="heuristic",
            reasoner_version=HEURISTIC_REASONER_VERSION,
            diagnosis=diagnosis,
        )
        return _wrap_port_report(output, tokens_used=None)

    # -- internals --------------------------------------------------------------

    def _confidence(
        self,
        diagnosis: dict[str, Any] | None,
        stats: dict[str, Any],
        revenue: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> float:
        """Deterministic report confidence — see ``_evidence_confidence``."""
        return _evidence_confidence(diagnosis, stats, revenue, candidates)

    def _escalation(
        self,
        confidence: float,
        *,
        diagnosis: dict[str, Any] | None,
        stats: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if diagnosis is None:
            reasons.append("no diagnosis available")
        if stats["window"]["total"] == 0:
            reasons.append("no payments in the incident window — evidence insufficient")
        if not candidates:
            reasons.append("no recovery candidates in scope")
        if confidence < self._threshold:
            reasons.append(
                f"confidence {confidence} below escalation threshold {self._threshold}"
            )
        return bool(reasons), reasons


class LlmError(RuntimeError):
    """The LLM path failed in a way the heuristic fallback must absorb."""


class LlmReasoner:
    """Optional OpenAI-compatible chat reasoner. Strictly validated; falls
    back to the heuristic reasoner on any failure, report marked degraded.

    ``chat_fn`` (tests) or the bundled httpx client must return an
    OpenAI-shaped chat.completion dict. The reasoner never receives secrets
    beyond the API key held in the HTTP client's headers, and never touches
    the DB or the gateway except through the whitelisted tools.
    """

    def __init__(
        self,
        tools: AgentTools,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        max_iterations: int = 6,
        max_attempts: int = 2,
        timeout_s: float = 30.0,
        chat_fn: Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]] | None = None,
        escalation_threshold: float = ESCALATION_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._tools = tools
        self._api_key = api_key
        self._model = model
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._max_iterations = max_iterations
        self._max_attempts = max_attempts
        self._timeout_s = timeout_s
        self._chat_fn = chat_fn
        self._threshold = escalation_threshold

    # -- ReasonerProto --------------------------------------------------------

    def investigate(self, evidence: EvidenceBundle) -> InvestigationReport:
        start_idx = len(self._tools.calls)
        tokens_used = 0
        last_error = "unknown"
        for attempt in range(1, self._max_attempts + 1):
            try:
                text, used = self._conversation(evidence, attempt_feedback=None if attempt == 1 else last_error)
                tokens_used += used
                output = self._build_output(text, evidence, start_idx)
                return _wrap_port_report(output, tokens_used=tokens_used or None)
            except LlmError as exc:
                last_error = str(exc)
                log.warning(
                    "llm investigation attempt failed",
                    extra={"attempt": attempt, "error": last_error, "incident_id": evidence.incident_id},
                )
        # Fall back to the deterministic reasoner; mark degraded.
        fallback = HeuristicReasoner(self._tools, escalation_threshold=self._threshold)
        report = fallback.investigate(evidence)
        structured = report.raw["structured"]
        structured["degraded"] = True
        structured["degraded_reasons"] = [
            *structured.get("degraded_reasons", []),
            f"llm investigation failed after {self._max_attempts} attempt(s): {last_error}",
            "fell back to the deterministic heuristic reasoner",
        ]
        structured["reasoner"] = "llm"
        structured["generated_by"] = f"{self._model} (fallback: heuristic)"
        structured["reasoner_version"] = LLM_REASONER_VERSION
        raw = dict(report.raw)
        raw["structured"] = structured
        raw["llm_fallback"] = True
        raw["llm_error"] = last_error
        if tokens_used:
            raw["tokens_used"] = tokens_used
        return dataclasses.replace(report, generated_by=structured["generated_by"], raw=raw)

    # -- conversation loop ------------------------------------------------------

    def _chat(self, messages: list[dict[str, Any]], specs: list[dict[str, Any]]) -> dict[str, Any]:
        if self._chat_fn is not None:
            return self._chat_fn(messages, specs)
        import httpx

        with httpx.Client(timeout=self._timeout_s) as client:
            resp = client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "messages": messages,
                    "tools": specs,
                    "temperature": 0,
                },
            )
            if resp.status_code != 200:
                raise LlmError(f"llm provider returned HTTP {resp.status_code}")
            return resp.json()

    def _conversation(
        self,
        evidence: EvidenceBundle,
        *,
        attempt_feedback: str | None,
    ) -> tuple[str, int]:
        """Run the bounded tool-calling loop; return (final_text, tokens_used)."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._user_prompt(evidence, attempt_feedback)},
        ]
        specs = self._tools.specs()
        tokens = 0
        violations = 0
        for _ in range(self._max_iterations):
            try:
                resp = self._chat(messages, specs)
            except LlmError:
                raise
            except Exception as exc:
                raise LlmError(f"llm transport error: {type(exc).__name__}: {exc}") from exc
            usage = resp.get("usage") or {}
            tokens += int(usage.get("total_tokens") or 0)
            try:
                message = resp["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LlmError("malformed chat completion response") from exc

            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content"),
                        "tool_calls": tool_calls,
                    }
                )
                for call in tool_calls:
                    fn = (call.get("function") or {})
                    name = str(fn.get("name") or "")
                    call_id = str(call.get("id") or "")
                    try:
                        raw_args = fn.get("arguments") or "{}"
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                        result = self._tools.call(name, args)
                        content = json.dumps(result.to_dict(), default=str)
                    except ToolNotAllowed as exc:
                        violations += 1
                        log.warning(
                            "llm requested a non-whitelisted tool",
                            extra={"tool": name, "incident_id": evidence.incident_id},
                        )
                        content = json.dumps({"error": str(exc), "allowed_tools": self._tools.tool_names})
                    except (ToolError, json.JSONDecodeError, TypeError) as exc:
                        content = json.dumps({"error": f"tool call failed: {exc}"})
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
                if violations >= 2:
                    raise LlmError("llm repeatedly requested non-whitelisted tools")
                continue

            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content, tokens
            messages.append({"role": "user", "content": "Produce the final JSON report now."})
        raise LlmError(f"no final answer within {self._max_iterations} iterations")

    # -- output construction ------------------------------------------------------

    def _build_output(
        self,
        text: str,
        evidence: EvidenceBundle,
        start_idx: int,
    ) -> InvestigationOutput:
        run_calls = self._tools.calls[start_idx:]
        tools_called = [c.name for c in run_calls]
        known_evidence: set[str] = set()
        tool_numbers: set[float] = set()
        known_targets: set[str] = set()
        for c in run_calls:
            known_evidence.update(c.evidence_ids)
            tool_numbers.update(collect_numbers(c.data))
            known_targets.update(collect_target_ids(c.data))
        known_targets |= known_evidence

        try:
            payload = extract_json(text)
        except ValueError as exc:
            raise LlmError(f"unparseable llm output: {exc}") from exc

        result = validate_llm_payload(
            payload,
            whitelisted_tools=set(self._tools.tool_names),
            tools_called=set(tools_called),
            known_evidence_ids=known_evidence,
            tool_numbers=tool_numbers,
            known_target_ids=known_targets,
        )
        if result.draft is None:
            raise LlmError("; ".join(result.errors) or "llm output failed validation")
        draft = result.draft

        # Assign canonical fact ids; drop inference refs to stripped facts.
        facts: list[ObservedFact] = []
        kept_llm_ids: set[str] = set()
        for i, f in enumerate(draft.observed_facts, start=1):
            fid = f"f{i}"
            kept_llm_ids.add(fid)
            facts.append(
                ObservedFact(
                    id=fid,
                    statement=f.statement,
                    tool=f.tool,
                    evidence_ids=list(f.evidence_ids),
                    data=dict(f.data),
                )
            )
        n_llm_facts = len(payload.get("observed_facts") or [])
        valid_refs = {f"f{i}" for i in range(1, n_llm_facts + 1)} & kept_llm_ids
        inferences = [
            AiInference(
                id=f"i{i}",
                statement=inf.statement,
                label=inf.label,
                confidence=inf.confidence,
                supporting_fact_ids=[r for r in inf.supporting_fact_ids if r in valid_refs],
            )
            for i, inf in enumerate(draft.ai_inferences, start=1)
        ]
        alternatives = [
            AlternativeHypothesis(rank=i, cause=h.cause, confidence=h.confidence, source="llm")
            for i, h in enumerate(draft.alternative_hypotheses, start=1)
        ]

        degraded_reasons = list(result.degraded_reasons)

        # An inference whose supporting facts were all stripped is an
        # unsupported claim: drop it rather than report it uncited.
        supported = [i for i in inferences if i.supporting_fact_ids]
        if len(supported) < len(inferences):
            degraded_reasons.append(
                f"dropped {len(inferences) - len(supported)} inference(s) with no surviving "
                "supporting facts"
            )
            inferences = [
                i.model_copy(update={"id": f"i{k}"}) for k, i in enumerate(supported, start=1)
            ]

        def _ensure_tool(name: str) -> ToolResult:
            nonlocal run_calls, tools_called
            call = next((c for c in run_calls if c.name == name), None)
            if call is None:
                call = self._tools.call(name)
                run_calls = self._tools.calls[start_idx:]
                tools_called = [c.name for c in run_calls]
            return call

        # Revenue implications come from the tool, not the model. If the model
        # never asked, the system asks on its behalf.
        revenue_call = _ensure_tool("get_revenue_at_risk")
        revenue_implications = _revenue_implications(revenue_call)

        # Evidence calibration weighs the same signals the heuristic reasoner
        # uses; the system fetches them when the model did not.
        stats_call = _ensure_tool("get_payment_stats")
        candidates_call = _ensure_tool("get_recovery_candidates")
        window_total = stats_call.data["window"]["total"]
        cand_list = candidates_call.data["candidates"]

        diagnosis_ctx = (evidence.context or {}).get("diagnosis")
        diagnosis_label = (diagnosis_ctx or {}).get("label")

        # Evidence-calibrated confidence ceiling: once the model's self-report
        # reaches the auto-execute floor, it may not exceed what the
        # deterministic formula supports on the same evidence.
        confidence = draft.confidence
        evidence_ceiling = _evidence_confidence(
            diagnosis_ctx, stats_call.data, revenue_call.data, cand_list
        )
        if confidence >= AUTO_EXECUTE_CONFIDENCE_FLOOR and evidence_ceiling < confidence:
            degraded_reasons.append(
                f"model confidence {confidence} exceeded the evidence-calibrated ceiling "
                f"{evidence_ceiling}; capped to the calibrated value"
            )
            confidence = evidence_ceiling

        uncertainties = list(draft.uncertainties)
        if 0 < window_total < THIN_EVIDENCE_WINDOW_FLOOR and not any(
            "thin" in u.lower() for u in uncertainties
        ):
            uncertainties.append(
                f"Only {window_total} payment(s) observed in the incident window; "
                "the evidence window is thin — treat conclusions as tentative."
            )
        if diagnosis_ctx is not None and float(diagnosis_ctx["confidence"]) < AUTO_EXECUTE_CONFIDENCE_FLOOR:
            uncertainties.append(
                f"Diagnosis confidence {round(float(diagnosis_ctx['confidence']), 4)} is below the "
                f"{AUTO_EXECUTE_CONFIDENCE_FLOOR} auto-execute floor — any automation takes the "
                "human-approval lane."
            )
        gate_confidence = _gate_confidence(confidence, diagnosis_ctx)
        if gate_confidence < confidence:
            uncertainties.append(
                f"Diagnosis class '{diagnosis_label}' is not auto-recoverable; a recovery proposal "
                f"here requires human approval regardless of model confidence (gate confidence "
                f"capped at {gate_confidence})."
            )

        # The recommended next step's amount and policy outcome are attached by
        # the SYSTEM via a real policy evaluation — never from the model. A
        # BLOCKED proposal is never presented as a recommendation: it is
        # dropped, flagged, and the headline falls back to escalation.
        model_action: RecommendedAction | None = None
        if draft.recommended_next_step is not None:
            step = draft.recommended_next_step
            try:
                preview = self._tools.call(
                    "propose_recovery_strategy",
                    {
                        "action_type": step.action_type,
                        "payment_id": step.payment_id,
                        "opportunity_id": step.opportunity_id,
                        "confidence": gate_confidence,
                    },
                )
                if preview.data["policy"]["outcome"] == "BLOCKED":
                    degraded_reasons.append(
                        f"recommended action {preview.data['action_type']!r} is BLOCKED by policy "
                        f"({', '.join(preview.data['policy']['rules_matched'])}); dropped and "
                        "replaced with escalate_human"
                    )
                else:
                    eff = revenue_call.data.get("expected_recovery_by_strategy", {}).get(
                        preview.data["action_type"]
                    )
                    model_action = _action_from_policy_preview(
                        preview,
                        rationale=step.rationale,
                        expected_recovery_paise=eff["point_paise"] if eff else None,
                    )
            except ToolError as exc:
                degraded_reasons.append(f"recommended action could not be policy-previewed: {exc}")

        # System-side escalation floor — independent of the model's self-report.
        escalation_reasons: list[str] = []
        if confidence < self._threshold:
            escalation_reasons.append(
                f"confidence {confidence} below escalation threshold {self._threshold}"
            )
        if not facts:
            escalation_reasons.append("no verifiable observed facts survived validation")
        if diagnosis_ctx is None:
            escalation_reasons.append("no diagnosis available")
        if window_total == 0:
            escalation_reasons.append("no payments in the incident window — evidence insufficient")
        if not cand_list and model_action is None:
            escalation_reasons.append("no recovery candidates in scope")
        if model_action is not None and model_action.action_type == ActionType.ESCALATE_HUMAN.value:
            escalation_reasons.append("the reasoner itself recommended human escalation")
        escalated = bool(escalation_reasons)

        def _safe_headline(action_type: ActionType) -> RecommendedAction:
            preview = self._tools.call(
                "propose_recovery_strategy",
                {"action_type": action_type.value, "confidence": confidence},
            )
            return _action_from_policy_preview(
                preview,
                rationale=_RATIONALE_BY_ACTION[action_type],
                expected_recovery_paise=None,
            )

        next_step: RecommendedAction | None = None
        secondary: RecommendedAction | None = None
        if escalated:
            if model_action is not None and model_action.action_type == ActionType.ESCALATE_HUMAN.value:
                next_step = model_action
            else:
                next_step = _safe_headline(ActionType.ESCALATE_HUMAN)
                secondary = model_action  # kept visible, but never headlined
        elif diagnosis_label == "no_fault":
            if model_action is not None and model_action.action_type != ActionType.NO_ACTION.value:
                degraded_reasons.append(
                    "diagnosis reports no actionable fault; the model's recovery proposal "
                    "was dropped in favor of no_action"
                )
                model_action = None
            if model_action is not None:
                next_step = model_action
            else:
                next_step = _safe_headline(ActionType.NO_ACTION)
        elif model_action is not None:
            next_step = model_action
        if next_step is None:
            # Nothing actionable survived validation: escalate rather than
            # leave the headline empty.
            escalated = True
            escalation_reasons.append("no actionable recommendation survived validation")
            next_step = _safe_headline(ActionType.ESCALATE_HUMAN)
        actions: list[RecommendedAction] = [a for a in (next_step, secondary) if a is not None]
        # The LLM proposes a single candidate; the ranked list mirrors what was
        # actually previewed: headline at rank 1, the kept-secondary at rank 2.
        ranked_candidates = [
            a.model_copy(update={"rank": r}) for r, a in enumerate(actions, start=1)
        ]

        run_calls = self._tools.calls[start_idx:]
        tools_called = [c.name for c in run_calls]

        return InvestigationOutput(
            incident_id=evidence.incident_id,
            what_happened=draft.what_happened,
            observed_facts=facts,
            ai_inferences=inferences,
            alternative_hypotheses=alternatives,
            revenue_implications=revenue_implications,
            recommended_actions=actions,
            recommended_next_step=next_step,
            recommended_candidates=ranked_candidates,
            uncertainties=uncertainties,
            confidence=confidence,
            escalated=escalated,
            escalation_reasons=escalation_reasons,
            degraded=bool(degraded_reasons),
            degraded_reasons=degraded_reasons,
            stripped_claims=list(result.stripped_claims),
            tools_called=tools_called,
            reasoner="llm",
            generated_by=self._model,
            reasoner_version=LLM_REASONER_VERSION,
            diagnosis=diagnosis_ctx,
        )

    # -- prompts ------------------------------------------------------------------

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are the PulseRecover investigation reasoner for a payments incident. "
            "You are ADVISORY ONLY: you never execute anything, and every financial "
            "action is decided by a separate deterministic policy engine.\n"
            "Rules you MUST follow:\n"
            "1. Establish facts ONLY by calling the provided tools. Never invent "
            "numbers: every amount, count, or rate you state must appear verbatim "
            "in a tool result (amounts are integer paise, currency INR).\n"
            "2. Only call tools from the provided list. Any other call is refused.\n"
            "3. When you have enough evidence, reply with ONE JSON object and nothing "
            "else, with keys: what_happened (string), observed_facts (array of "
            "{statement, tool, evidence_ids, data}), ai_inferences (array of "
            "{statement, label, confidence, supporting_fact_ids} where "
            "supporting_fact_ids reference your facts as f1, f2, ... in order), "
            "alternative_hypotheses (array of {cause, confidence}), "
            "recommended_next_step ({action_type, rationale, payment_id or "
            "opportunity_id} or null), uncertainties (array of strings), "
            "confidence (0..1).\n"
            "4. Money claims in free text are checked against tool results; "
            "unverifiable claims are removed and the report is flagged degraded.\n"
            "5. Use request_payment_link / request_recovery_execution sparingly and "
            "only for a clearly recoverable failed payment; the policy engine will "
            "gate whatever you propose.\n"
            "6. Never advocate execution: do not ask the operator to auto-execute, "
            "skip approval, or bypass the policy gate. State what you recommend and "
            "why; the system attaches the policy engine's live outcome to your "
            "recommendation. `refund` is never available — proposing blocked "
            "actions only gets them dropped and the report degraded.\n"
            "7. Be honest about evidence: when the incident window holds few "
            "payments, say so in uncertainties and lower your confidence. When the "
            "diagnosis confidence is below the 0.85 auto-execute floor, say so — "
            "automation then takes the human-approval lane. Do not report "
            "confidence above what the evidence supports: the system caps it at "
            "the evidence-calibrated ceiling."
        )

    @staticmethod
    def _user_prompt(evidence: EvidenceBundle, attempt_feedback: str | None) -> str:
        prompt = (
            f"Investigate incident {evidence.incident_id} (metric: {evidence.metric}).\n"
            f"Window: {evidence.window_start} .. {evidence.window_end}.\n"
            f"Diagnosis context: {json.dumps((evidence.context or {}).get('diagnosis'), default=str)}\n"
            "Start by calling get_incident, get_payment_stats, get_failure_distribution, "
            "get_revenue_at_risk and get_recovery_candidates, then produce the JSON report."
        )
        if attempt_feedback:
            prompt += (
                "\n\nYour previous attempt was rejected: "
                + attempt_feedback
                + "\nReturn ONLY the JSON object, following the schema exactly."
            )
        return prompt


def choose_reasoner(
    tools: AgentTools,
    *,
    llm_provider: str,
    openai_api_key: str,
    openai_base_url: str = "",
    openai_model: str = "gpt-4o-mini",
    pollinations_api_key: str = "",
    pollinations_base_url: str = "https://gen.pollinations.ai/v1",
    pollinations_model: str = "openai",
):
    """Default reasoner selection: LLM only when explicitly configured."""
    if llm_provider == "openai" and openai_api_key:
        return LlmReasoner(
            tools,
            api_key=openai_api_key,
            model=openai_model,
            base_url=openai_base_url or None,
        )
    if llm_provider == "pollinations" and pollinations_api_key:
        return LlmReasoner(
            tools,
            api_key=pollinations_api_key,
            model=pollinations_model,
            base_url=pollinations_base_url or "https://gen.pollinations.ai/v1",
        )
    return HeuristicReasoner(tools)


__all__ = [
    "HeuristicReasoner",
    "LlmError",
    "LlmReasoner",
    "choose_reasoner",
]

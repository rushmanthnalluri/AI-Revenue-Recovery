# PulseRecover — AI Investigation Agent

> **Default mode is a deterministic, offline heuristic reasoner. The LLM
> reasoner is optional and advisory only.** Neither mode can move money:
> probabilistic AI proposes, the deterministic policy engine decides, payment
> infrastructure executes, verification proves (ADR 0003/0004).

The investigation agent turns a detected incident into a structured, auditable
report: what happened (observed facts with evidence ids), what it probably
means (labeled inferences with confidence), what it is worth (revenue
implications from the revenue engine), and what to do next (a recommendation
whose policy outcome is previewed by the real PolicyEngine).

## Architecture

```
POST /api/v1/incidents/{id}/investigate
        │
        ▼
AgentService (app/services/agent/service.py)
  1. load incident (404 if unknown)
  2. return latest completed report unless force_refresh
  3. ensure diagnosis (DiagnosisService.classify if missing;
     a diagnosis failure becomes an uncertainty, never an abort)
  4. build EvidenceBundle + AgentTools(session, incident_id)
  5. reasoner = LlmReasoner if LLM_PROVIDER=openai AND OPENAI_API_KEY
                else HeuristicReasoner            ← default
  6. persist agent_reports row (input, output, model, tokens, duration_ms)
  7. audit_logs row: actor=agent:investigator, action=agent.investigate
        │
        ▼
Reasoner (app/services/agent/reasoners.py) ──► AgentTools (tools.py)
        │                                        │
        │                              read tools over services/db
        │                              request_* tools → PolicyEngine
        ▼                                        │
InvestigationOutput (report.py) ◄── validation.py (LLM only:
  observed_facts / ai_inferences /               schema + hallucination guard)
  recommended_actions (+policy preview) /
  revenue_implications / uncertainties /
  confidence / escalation
```

## Trust boundary

- **All financial facts originate from the tool layer** (`AgentTools`), never
  from the reasoner. Every `ToolResult` carries `evidence_ids` (the DB row ids
  it was derived from), and every observed fact cites the tool name plus those
  ids — every claim is traceable.
- **Tool whitelist.** Reasoners invoke tools only through
  `AgentTools.call(name, args)`. Any other name raises `ToolNotAllowed`; there
  is no `getattr`/arbitrary-callable path. An LLM that requests a rogue tool
  gets a refusal message (with the allowed list); two violations abort the run.
- **The reasoner never receives secrets, raw DB access, or gateway access.**
  The OpenAI key lives only in the LLM HTTP client's Authorization header.
- **Mutation path = exactly two tools.** `request_payment_link` and
  `request_recovery_execution` create a `recovery_actions` row in status
  `PROPOSED` — the amount is copied from the ORIGINAL payment/opportunity row
  (callers cannot supply an amount) — and immediately evaluate it through
  `PolicyEngine.evaluate`, returning the decision verbatim. The action row is
  then moved to `POLICY_EVALUATED` (ALLOWED), `PENDING_APPROVAL`
  (REQUIRES_APPROVAL), or `REJECTED` (BLOCKED), linked to the persisted
  `policy_decisions` row. **They never call the gateway** — execution belongs
  to the recovery executor, which re-checks policy and approval state.
- `propose_recovery_strategy` is a read-only dry run of the same gate (a
  preview; creates no action row). This is how the report's
  `recommended_next_step` gets its `policy_preview`: attached by the system
  from a real evaluation, never written by the AI.

## Tool reference

All tools are bound to one incident per `AgentTools` instance. Money is
integer paise, currency INR.

| Tool | Args | Mutates? | Returns |
|---|---|---|---|
| `get_incident` | – | no | incident fields, detection provenance, latest diagnosis context |
| `get_payment_stats` | – | no | window vs baseline counts/amounts/failure rates + sampled failed payment ids |
| `get_failure_distribution` | – | no | failed payments grouped by error source/reason/method/failure class |
| `get_customer_history` | `customer_id` | no | payment counts, captured volume, `opted_out` flag |
| `get_revenue_at_risk` | – | no | counterfactual observed loss / recoverable / expected recovery per strategy / verified recovered (RevenueService) |
| `get_recovery_candidates` | – | no | existing `recovery_opportunities`, or candidates derived read-only from failed payments |
| `propose_recovery_strategy` | `action_type`, `payment_id`/`opportunity_id`, `confidence?` | no (persists only the `policy_decisions` record) | policy decision verbatim |
| `request_payment_link` | `payment_id`/`opportunity_id`, `confidence?`, `note?` | creates PROPOSED `create_payment_link` action + opportunity if needed | action id, status, policy decision verbatim |
| `request_recovery_execution` | `action_type`, target, `confidence?`, `note?` | creates PROPOSED action of the given type | same |

Every policy evaluation (preview or request) is persisted to
`policy_decisions`; every `request_*` also writes an
`agent.action_requested` audit row, and BLOCKED decisions are additionally
audited by the policy engine itself.

## Report shape

`agent_reports.output` (and `GET /api/v1/incidents/{id}/investigation`)
cleanly separates what the UI renders differently:

- `observed_facts[]` — deterministic tool output: `{id, statement, tool,
  evidence_ids, data}`.
- `ai_inferences[]` — probabilistic claims: `{id, statement, label,
  confidence, supporting_fact_ids}` referencing facts.
- `alternative_hypotheses[]` — ranked, from the diagnosis top-3.
- `revenue_implications` — copied from the revenue engine tool result.
- `recommended_actions[]` / `recommended_next_step` — `{action_type,
  rationale, amount_paise, payment_id/opportunity_id,
  expected_recovery_paise, policy_preview}`.
- `uncertainties[]`, `confidence`, `escalated` + `escalation_reasons`,
  `degraded` + `degraded_reasons`, `stripped_claims[]`, `tools_called[]`.

## HeuristicReasoner (default)

Offline, deterministic: same DB state → byte-identical report (policy
previews exclude timestamps precisely so this holds). Confidence is a fixed
formula: diagnosis confidence (0.30 if none) +0.10 with ≥10 failed payments,
−0.20 with zero, −0.10 for a low-confidence revenue estimate, −0.10 with no
candidates, clamped to [0.05, 0.95]. It **escalates** (`escalated=true`,
recommends `escalate_human`) when confidence < 0.5, when no diagnosis exists,
or when the incident window has no payments.

Action choice maps the dominant failure class to the least-aggressive
effective action: transient classes (`timeout`, `soft_decline`) →
`retry_payment`; intent/funds classes → `create_payment_link`;
permanent/unknown → `notify_customer`; insufficient evidence →
`escalate_human`.

## LlmReasoner (optional, advisory)

Enabled only when `LLM_PROVIDER=openai` **and** `OPENAI_API_KEY` are set
(`OPENAI_BASE_URL`, `OPENAI_MODEL` optional). It runs a bounded tool-calling
loop (max 6 iterations, max 2 attempts) against an OpenAI-compatible
`/chat/completions` endpoint via httpx, then strictly validates the output:

1. **JSON extraction + schema validation** — malformed output is rejected;
   the attempt is retried once with the error fed back.
2. **Hallucination guard** — every numeric financial claim must exactly match
   a number from a tool result in the same run: money-ish JSON keys
   (`*_paise`, `amount*`, `revenue`, ...), every number inside a fact's
   `data` payload, and free-text money phrases (`₹…`, `Rs 999 crore`,
   `INR 2 lakh`, bare ≥7-digit integers). Unverifiable claims are **stripped
   and flagged** (`stripped_claims`, `degraded=true`). Facts citing tools
   that were never called or unknown evidence ids are stripped too.
3. **System-attached numbers** — `revenue_implications` and the recommended
   action's `policy_preview`/amount are attached by the system from real tool
   calls, never taken from model text.
4. **Fallback** — if validation fails after the retry, the run falls back to
   the HeuristicReasoner and the report is marked degraded
   (`generated_by: "<model> (fallback: heuristic)"`).

Even a fully-compliant LLM run changes nothing about execution: its
`request_*` calls land as PROPOSED rows behind the same deterministic gate.

## API

- `POST /api/v1/incidents/{incident_id}/investigate` (X-API-Key required)
  body: `{"force_refresh": false}`. Runs diagnosis if missing, investigates,
  persists + audits the run, returns the full report. Idempotent: without
  `force_refresh`, an existing completed report is returned.
- `GET /api/v1/incidents/{incident_id}/investigation` — latest completed
  report; 404 when none exists.

## Guardrails summary

- whitelist-only tool dispatch; two rogue LLM tool calls abort the run
- amounts only ever copied from original payment/opportunity rows
- every proposal gated by the deterministic PolicyEngine; decision persisted
- the agent never calls the gateway; execution is the executor's job
- hallucination guard strips unverifiable financial claims and flags the report
- low confidence or thin evidence → `escalate_human` recommendation
- every run persisted in `agent_reports` and mirrored into `audit_logs`

/**
 * Typed mirror of the AI investigation report contract
 * (backend/app/schemas/agent.py — InvestigationReportView / InvestigateResponse).
 *
 * The API client types these endpoints as JsonObject, so this module also
 * provides a defensive parser: it validates the fields the UI depends on and
 * normalizes anything optional, never inventing values.
 */

export interface ObservedFact {
  id: string;
  statement: string;
  tool: string;
  evidence_ids: string[];
  data: Record<string, unknown>;
}

export interface AiInference {
  id: string;
  statement: string;
  label: string;
  confidence: number;
  supporting_fact_ids: string[];
}

export interface AlternativeHypothesis {
  rank: number;
  cause: string;
  confidence: number;
  source: string;
}

export type PolicyOutcomeValue = "ALLOWED" | "REQUIRES_APPROVAL" | "BLOCKED" | string;

export interface PolicyPreview {
  outcome: PolicyOutcomeValue;
  reasons: string[];
  rules_matched: string[];
  policy_version: string;
}

export interface RecommendedAction {
  action_type: string;
  rationale: string;
  amount_paise: number | null;
  currency: string;
  payment_id: string | null;
  opportunity_id: string | null;
  expected_recovery_paise: number | null;
  policy_preview: PolicyPreview | null;
}

export interface RevenueImplications {
  currency: string;
  observed_loss_point_paise: number | null;
  observed_loss_lower_paise: number;
  observed_loss_upper_paise: number;
  recoverable_point_paise: number | null;
  recoverable_lower_paise: number;
  recoverable_upper_paise: number;
  actual_recovered_paise: number;
  recovered_actions_count: number;
  confidence: number;
  low_confidence: boolean;
  basis: string;
}

export interface InvestigationReport {
  id: string;
  incident_id: string;
  summary: string;
  observed_facts: ObservedFact[];
  ai_inferences: AiInference[];
  recommended_actions: RecommendedAction[];
  recommended_next_step: RecommendedAction | null;
  alternative_hypotheses: AlternativeHypothesis[];
  revenue_implications: RevenueImplications | null;
  uncertainties: string[];
  confidence: number;
  escalated: boolean;
  escalation_reasons: string[];
  degraded: boolean;
  degraded_reasons: string[];
  stripped_claims: Record<string, unknown>[];
  tools_called: string[];
  reasoner: string;
  generated_by: string;
  tokens_used: number | null;
  duration_ms: number | null;
  created_at: string;
}

export interface InvestigateResponse {
  report_id: string;
  incident_id: string;
  status: string; // running | completed | failed
  started_at: string;
  report: InvestigationReport | null;
}

// ---------------------------------------------------------------------------
// defensive parsing
// ---------------------------------------------------------------------------

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function str(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}

function num(v: unknown, fallback = 0): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

function numOrNull(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function bool(v: unknown): boolean {
  return v === true;
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

function parseAction(v: unknown): RecommendedAction | null {
  if (!isRecord(v)) return null;
  const preview = isRecord(v.policy_preview) ? v.policy_preview : null;
  return {
    action_type: str(v.action_type, "unknown"),
    rationale: str(v.rationale),
    amount_paise: numOrNull(v.amount_paise),
    currency: str(v.currency, "INR"),
    payment_id: typeof v.payment_id === "string" ? v.payment_id : null,
    opportunity_id: typeof v.opportunity_id === "string" ? v.opportunity_id : null,
    expected_recovery_paise: numOrNull(v.expected_recovery_paise),
    policy_preview: preview
      ? {
          outcome: str(preview.outcome, "unknown"),
          reasons: strList(preview.reasons),
          rules_matched: strList(preview.rules_matched),
          policy_version: str(preview.policy_version, "unknown"),
        }
      : null,
  };
}

function parseFact(v: unknown): ObservedFact | null {
  if (!isRecord(v) || typeof v.statement !== "string") return null;
  return {
    id: str(v.id),
    statement: v.statement,
    tool: str(v.tool, "unknown"),
    evidence_ids: strList(v.evidence_ids),
    data: isRecord(v.data) ? v.data : {},
  };
}

function parseInference(v: unknown): AiInference | null {
  if (!isRecord(v) || typeof v.statement !== "string") return null;
  return {
    id: str(v.id),
    statement: v.statement,
    label: str(v.label, "inference"),
    confidence: num(v.confidence),
    supporting_fact_ids: strList(v.supporting_fact_ids),
  };
}

function parseHypothesis(v: unknown): AlternativeHypothesis | null {
  if (!isRecord(v) || typeof v.cause !== "string") return null;
  return {
    rank: num(v.rank),
    cause: v.cause,
    confidence: num(v.confidence),
    source: str(v.source, "diagnosis"),
  };
}

function parseRevenue(v: unknown): RevenueImplications | null {
  if (!isRecord(v)) return null;
  return {
    currency: str(v.currency, "INR"),
    observed_loss_point_paise: numOrNull(v.observed_loss_point_paise),
    observed_loss_lower_paise: num(v.observed_loss_lower_paise),
    observed_loss_upper_paise: num(v.observed_loss_upper_paise),
    recoverable_point_paise: numOrNull(v.recoverable_point_paise),
    recoverable_lower_paise: num(v.recoverable_lower_paise),
    recoverable_upper_paise: num(v.recoverable_upper_paise),
    actual_recovered_paise: num(v.actual_recovered_paise),
    recovered_actions_count: num(v.recovered_actions_count),
    confidence: num(v.confidence),
    low_confidence: bool(v.low_confidence),
    basis: str(v.basis),
  };
}

function list<T>(v: unknown, parse: (x: unknown) => T | null): T[] {
  if (!Array.isArray(v)) return [];
  const out: T[] = [];
  for (const item of v) {
    const parsed = parse(item);
    if (parsed !== null) out.push(parsed);
  }
  return out;
}

/**
 * Parse the GET /investigation payload. Returns null when the payload is not
 * a recognizable report so the caller can show an error instead of fake data.
 */
export function parseInvestigationReport(raw: unknown): InvestigationReport | null {
  if (!isRecord(raw)) return null;
  if (typeof raw.id !== "string" || typeof raw.summary !== "string") return null;
  return {
    id: raw.id,
    incident_id: str(raw.incident_id),
    summary: raw.summary,
    observed_facts: list(raw.observed_facts, parseFact),
    ai_inferences: list(raw.ai_inferences, parseInference),
    recommended_actions: list(raw.recommended_actions, parseAction),
    recommended_next_step: parseAction(raw.recommended_next_step),
    alternative_hypotheses: list(raw.alternative_hypotheses, parseHypothesis),
    revenue_implications: parseRevenue(raw.revenue_implications),
    uncertainties: strList(raw.uncertainties),
    confidence: num(raw.confidence),
    escalated: bool(raw.escalated),
    escalation_reasons: strList(raw.escalation_reasons),
    degraded: bool(raw.degraded),
    degraded_reasons: strList(raw.degraded_reasons),
    stripped_claims: Array.isArray(raw.stripped_claims)
      ? raw.stripped_claims.filter(isRecord)
      : [],
    tools_called: strList(raw.tools_called),
    reasoner: str(raw.reasoner, "heuristic"),
    generated_by: str(raw.generated_by, "heuristic"),
    tokens_used: numOrNull(raw.tokens_used),
    duration_ms: numOrNull(raw.duration_ms),
    created_at: str(raw.created_at),
  };
}

/**
 * Parse the POST /investigate response. The report body reuses the same
 * InvestigationReportView shape; status flags async completion.
 */
export function parseInvestigateResponse(raw: unknown): InvestigateResponse | null {
  if (!isRecord(raw)) return null;
  return {
    report_id: str(raw.report_id),
    incident_id: str(raw.incident_id),
    status: str(raw.status, "completed"),
    started_at: str(raw.started_at),
    report: parseInvestigationReport(raw.report),
  };
}

/** Short evidence/id chip label: first 8 chars, enough to correlate. */
export function shortRef(id: string): string {
  return id.length <= 8 ? id : id.slice(0, 8);
}

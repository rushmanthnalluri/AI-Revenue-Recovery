/**
 * Recovery Planner / Approval Center — view-level contract.
 *
 * `api.recovery.get()` / `.plan()` and the mutation endpoints are typed as
 * `JsonObject` in lib/types.ts (the hand-written mirror lags the finished
 * backend). The backend is complete and returns the shapes in
 * contracts/openapi.json (OpportunityDetail, RecoveryPlan, ActionResponse).
 * The type aliases below re-export those schemas from lib/types.ts (the
 * single source of truth); the parse helpers coerce the unknown payload
 * defensively so a partially-populated response degrades instead of crashing
 * the screen.
 *
 * This file is scoped to the recovery screens.
 */

import type {
  ActionResponse,
  ActionType,
  AuditRef,
  OpportunityDetail,
  PolicyDecisionView,
  PolicyOutcome,
  RecoveryActionView,
  RecoveryPlan,
  RecoveryStatus,
  StrategyOption,
} from "@/lib/types";

export type { PolicyOutcome };

export type StrategyOptionView = StrategyOption;
export type RecoveryPlanView = RecoveryPlan;
export type PolicyDecisionItem = PolicyDecisionView;
export type RecoveryActionItem = RecoveryActionView;
export type AuditRefItem = AuditRef;
export type OpportunityDetailView = OpportunityDetail;
export type ActionResponseView = ActionResponse;

// ---------------------------------------------------------------------------
// defensive coercion helpers
// ---------------------------------------------------------------------------

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function strOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function bool(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function strList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

const RECOVERY_STATUSES: RecoveryStatus[] = [
  "PROPOSED",
  "POLICY_EVALUATED",
  "PENDING_APPROVAL",
  "APPROVED",
  "SCHEDULED",
  "REJECTED",
  "EXECUTING",
  "VERIFYING",
  "RECOVERED",
  "FAILED",
  "UNKNOWN",
  "CANCELLED",
  "ESCALATED",
];

function status(value: unknown): RecoveryStatus {
  const s = str(value);
  return (RECOVERY_STATUSES as string[]).includes(s) ? (s as RecoveryStatus) : "PROPOSED";
}

function outcome(value: unknown): PolicyOutcome {
  const s = str(value);
  return s === "ALLOWED" || s === "BLOCKED" || s === "REQUIRES_APPROVAL" ? s : "BLOCKED";
}

/** Backend stamps `environment or "research"` on read — mirror that fallback. */
function environment(value: unknown): "real_test" | "research" {
  return value === "real_test" ? "real_test" : "research";
}

// ---------------------------------------------------------------------------
// parsers
// ---------------------------------------------------------------------------

export function parsePolicyDecision(value: unknown): PolicyDecisionItem | null {
  if (typeof value !== "object" || value === null) return null;
  const r = asRecord(value);
  return {
    id: str(r.id),
    outcome: outcome(r.outcome),
    reasons: strList(r.reasons),
    rules_matched: strList(r.rules_matched),
    policy_version: str(r.policy_version, "unknown"),
    decided_at: str(r.decided_at),
  };
}

export function parseStrategy(value: unknown): StrategyOptionView {
  const r = asRecord(value);
  return {
    id: str(r.id),
    action_type: str(r.action_type, "no_action") as ActionType,
    rank: num(r.rank),
    expected_recovery_paise: num(r.expected_recovery_paise),
    confidence: num(r.confidence),
    risk: str(r.risk, "low"),
    eligibility: bool(r.eligibility, true),
    reason: strOrNull(r.reason),
    constraints: asRecord(r.constraints),
    generated_by: str(r.generated_by, "heuristic"),
    selected: bool(r.selected),
  };
}

export function parsePlan(value: unknown): RecoveryPlanView {
  const r = asRecord(value);
  const previewRaw = r.policy_preview;
  const preview =
    typeof previewRaw === "object" && previewRaw !== null
      ? {
          outcome: outcome(asRecord(previewRaw).outcome),
          reasons: strList(asRecord(previewRaw).reasons),
        }
      : null;
  return {
    opportunity_id: str(r.opportunity_id),
    strategies: Array.isArray(r.strategies) ? r.strategies.map(parseStrategy) : [],
    recommended_strategy_id: strOrNull(r.recommended_strategy_id),
    policy_preview: preview,
  };
}

export function parseAction(value: unknown): RecoveryActionItem {
  const r = asRecord(value);
  return {
    id: str(r.id),
    opportunity_id: str(r.opportunity_id),
    strategy_id: strOrNull(r.strategy_id),
    action_type: str(r.action_type, "no_action") as ActionType,
    status: status(r.status),
    amount_paise: num(r.amount_paise),
    currency: str(r.currency, "INR"),
    confidence: num(r.confidence),
    actor: str(r.actor),
    attempts: num(r.attempts),
    gateway_request_id: strOrNull(r.gateway_request_id),
    policy_decision: parsePolicyDecision(r.policy_decision),
    proposed_at: str(r.proposed_at),
    executed_at: strOrNull(r.executed_at),
    verified_at: strOrNull(r.verified_at),
    completed_at: strOrNull(r.completed_at),
    approved_by: strOrNull(r.approved_by),
    note: strOrNull(r.note),
    last_error: strOrNull(r.last_error),
  };
}

export function parseAuditRef(value: unknown): AuditRefItem {
  const r = asRecord(value);
  return {
    id: str(r.id),
    actor: str(r.actor),
    action: str(r.action),
    entity_type: str(r.entity_type),
    entity_id: str(r.entity_id),
    request_id: strOrNull(r.request_id),
    details: asRecord(r.details),
    created_at: str(r.created_at),
  };
}

export function parseOpportunityDetail(value: unknown): OpportunityDetailView {
  const r = asRecord(value);
  return {
    id: str(r.id),
    incident_id: strOrNull(r.incident_id),
    payment_id: strOrNull(r.payment_id),
    customer_id: strOrNull(r.customer_id),
    subscription_id: strOrNull(r.subscription_id),
    opportunity_type: str(r.opportunity_type, "unknown"),
    status: status(r.status),
    amount_paise: num(r.amount_paise),
    currency: str(r.currency, "INR"),
    expected_recovery_paise: num(r.expected_recovery_paise),
    confidence: num(r.confidence),
    risk: str(r.risk, "low"),
    reason: strOrNull(r.reason),
    created_at: str(r.created_at),
    expires_at: strOrNull(r.expires_at),
    environment: environment(r.environment),
    constraints: asRecord(r.constraints),
    actions: Array.isArray(r.actions) ? r.actions.map(parseAction) : [],
    audit: Array.isArray(r.audit) ? r.audit.map(parseAuditRef) : [],
  };
}

export function parseActionResponse(value: unknown): ActionResponseView {
  const r = asRecord(value);
  return {
    action_id: strOrNull(r.action_id),
    opportunity_id: str(r.opportunity_id),
    status: status(r.status),
    message: str(r.message),
    policy_decision: parsePolicyDecision(r.policy_decision),
  };
}

// ---------------------------------------------------------------------------
// display helpers
// ---------------------------------------------------------------------------

const ACTION_LABELS: Record<string, string> = {
  retry_payment: "Retry payment",
  create_payment_link: "Create payment link",
  notify_customer: "Notify customer",
  extend_grace_period: "Extend grace period",
  pause_subscription: "Pause subscription",
  resume_subscription: "Resume subscription",
  refund: "Refund",
  escalate_human: "Escalate to human",
  no_action: "No action",
};

/** snake_case action type → human label. Unknown values get title-cased. */
export function actionTypeLabel(actionType: string): string {
  return (
    ACTION_LABELS[actionType] ??
    actionType.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

const OPPORTUNITY_TYPE_LABELS: Record<string, string> = {
  failed_payment_retry: "Failed payment retry",
  dropped_checkout: "Dropped checkout",
  stuck_checkout_payment: "Stuck checkout",
};

export function opportunityTypeLabel(opportunityType: string): string {
  return (
    OPPORTUNITY_TYPE_LABELS[opportunityType] ??
    opportunityType.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

/** failure_class arrives as an enum value like "insufficient_funds". */
export function failureClassLabel(failureClass: string): string {
  return failureClass.replace(/_/g, " ");
}

/**
 * Failure class is a plan-time fact: the strategy generator derives one class
 * per opportunity and records it on the `recovery.strategies_generated` audit
 * row's details (it is not part of the summary or strategy-row contract).
 */
export function detailFailureClass(detail: OpportunityDetailView | undefined): string | null {
  if (!detail) return null;
  for (const row of detail.audit ?? []) {
    if (row.action === "recovery.strategies_generated") {
      const fc = row.details?.failure_class;
      if (typeof fc === "string" && fc) return fc;
    }
  }
  return null;
}

/** The action that currently drives the opportunity's projected status. */
export function latestAction(detail: OpportunityDetailView): RecoveryActionItem | null {
  const actions = detail.actions ?? [];
  if (actions.length === 0) return null;
  return actions[actions.length - 1] ?? null;
}

/**
 * One-line state semantics for a recovery action, so an attempted action is
 * never confused with recovered money: only RECOVERED is verification-sourced
 * (webhook/inline verify against gateway truth); everything before it is a
 * promise, not an outcome. Returns null for states that need no disambiguation
 * (UNKNOWN carries its own richer explainer; terminal FAILED shows last_error).
 */
export function actionStateNote(status: RecoveryStatus): string | null {
  switch (status) {
    case "PROPOSED":
    case "POLICY_EVALUATED":
      return "Proposed only — nothing has fired.";
    case "PENDING_APPROVAL":
      return "Waiting on a human decision — nothing has fired.";
    case "APPROVED":
      return "Approved — cleared to fire, not yet executed.";
    case "SCHEDULED":
      return "Parked until due — pre-execution; nothing has reached the gateway.";
    case "EXECUTING":
      return "Attempt fired at the gateway — outcome not yet verified.";
    case "VERIFYING":
      return "Verifying the outcome against gateway records — not yet counted as recovered.";
    case "RECOVERED":
      return "Verified against gateway truth — counted in recovered revenue.";
    default:
      return null;
  }
}

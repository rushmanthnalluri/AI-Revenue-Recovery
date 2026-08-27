/**
 * PulseRecover API contract types.
 *
 * Hand-written mirror of contracts/openapi.json (PulseRecover API 0.1.0).
 * A later wave may replace this with generated types; until then, keep this
 * file in sync with the contract. Conventions: money is integer paise (INR),
 * datetimes are ISO 8601 strings, enums are uppercase string unions.
 *
 * Endpoints whose contract response schema is `{}` (still being built out by
 * backend agents) are typed as `JsonObject`.
 */

export type ISODateTime = string;
export type JsonObject = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

export type Severity = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";

export type IncidentStatus =
  | "OPEN"
  | "INVESTIGATING"
  | "DIAGNOSED"
  | "RECOVERING"
  | "RESOLVED"
  | "CLOSED"
  | "FALSE_POSITIVE";

export type RecoveryStatus =
  | "PROPOSED"
  | "POLICY_EVALUATED"
  | "PENDING_APPROVAL"
  | "APPROVED"
  | "REJECTED"
  | "EXECUTING"
  | "VERIFYING"
  | "RECOVERED"
  | "FAILED"
  | "UNKNOWN"
  | "CANCELLED"
  | "ESCALATED";

export type Granularity = "minute" | "hour" | "day";

export type ActionType =
  | "retry_payment"
  | "create_payment_link"
  | "notify_customer"
  | "extend_grace_period"
  | "pause_subscription"
  | "resume_subscription"
  | "refund"
  | "escalate_human"
  | "no_action";

export type PolicyOutcome = "ALLOWED" | "BLOCKED" | "REQUIRES_APPROVAL";

// ---------------------------------------------------------------------------
// Error envelope (backend/app/main.py `_error`) + FastAPI validation fallback
// ---------------------------------------------------------------------------

export interface ApiErrorEnvelope {
  error: {
    code: string;
    message: string;
    request_id: string | null;
  };
}

export interface ValidationError {
  loc: (string | number)[];
  msg: string;
  type: string;
  input?: unknown;
  ctx?: Record<string, unknown>;
}

export interface HTTPValidationError {
  detail: ValidationError[];
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export interface HealthResponse {
  status: string;
}

export interface ComponentHealth {
  status: string;
  detail?: string | null;
}

export interface SystemHealth {
  status: string;
  version: string;
  app_env: string;
  simulation_mode: boolean;
  time?: ISODateTime;
  checks?: Record<string, ComponentHealth>;
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

export interface DashboardSummary {
  currency: string;
  generated_at?: ISODateTime;
  open_incidents: number;
  incidents_by_severity?: Record<string, number>;
  revenue_at_risk_paise: number;
  recovered_revenue_paise: number;
  lost_revenue_paise: number;
  recovery_rate: number;
  active_recoveries: number;
  payments_success_rate: number;
  payments_observed: number;
  pending_approvals: number;
  /** Baseline-window success rate; null when the baseline has no data. */
  payments_baseline_success_rate?: number | null;
  /** Recoverable share of the at-risk loss (counterfactual x recoverability). */
  recoverable_revenue_paise?: number;
  /** True when any open incident's point estimate is low-confidence. */
  revenue_at_risk_low_confidence?: boolean;
  recent_incidents?: IncidentSummary[];
}

export interface TimeSeriesPoint {
  ts: ISODateTime;
  value: number;
}

export interface DashboardTimeseries {
  metric: string;
  granularity: Granularity;
  currency: string;
  points: TimeSeriesPoint[];
}

// ---------------------------------------------------------------------------
// Incidents
// ---------------------------------------------------------------------------

export interface IncidentSummary {
  id: string;
  title: string;
  status: IncidentStatus;
  severity: Severity;
  metric: string;
  detection_method: string;
  detected_at: ISODateTime;
  baseline_value?: number | null;
  observed_value?: number | null;
  deviation_pct?: number | null;
  affected_payments_count: number;
  revenue_at_risk_paise: number;
  currency: string;
}

export interface IncidentListResponse {
  items: IncidentSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface IncidentTimelineEvent {
  ts: ISODateTime;
  kind: string; // detected | status_change | evidence_added | diagnosis | action | note
  summary: string;
  actor: string;
  details?: Record<string, unknown>;
}

export interface EvidenceItem {
  id: string;
  evidence_type: string;
  title: string;
  payload: Record<string, unknown>;
  collector: string;
  collected_at: ISODateTime;
}

export interface DiagnosisView {
  id: string;
  model_name: string;
  model_version: string;
  predicted_cause: string;
  confidence: number;
  explanation?: string | null;
  created_at: ISODateTime;
}

export interface EstimateView {
  point_paise?: number | null;
  lower_paise: number;
  upper_paise: number;
  confidence: number;
  low_confidence: boolean;
  basis?: string;
}

export interface FailureClassView {
  failure_class: string;
  failed_count: number;
  failed_amount_paise: number;
  allocated_loss: EstimateView;
  recoverability_factor: number;
  recoverable: EstimateView;
}

export interface RevenueBreakdown {
  currency: string;
  window_start: ISODateTime;
  window_end: ISODateTime;
  baseline_start: ISODateTime;
  baseline_end: ISODateTime;
  observed_loss: EstimateView;
  recoverable: EstimateView;
  expected_recovery_by_strategy?: Record<string, EstimateView>;
  actual_recovered_paise: number;
  recovered_actions_count: number;
  failure_classes?: FailureClassView[];
}

export interface InsightsOutlier {
  dimension: string;
  value: string;
  basis: "failure_rate" | "failure_share";
  incident_rate: number;
  baseline_rate: number;
  /** null = facet absent at baseline ("new"), ranks above any finite lift */
  lift?: number | null;
  support: number;
  window_group_size: number;
  baseline_group_size: number;
  low_confidence: boolean;
}

export interface PlatformCallout {
  dimension: string;
  value: string;
  classification: "platform_wide" | "incident_specific";
  /** single-merchant sim reality: the benchmark is the simulated fleet */
  platform_scope: string;
  platform_window_rate: number;
  platform_baseline_rate: number;
  platform_lift?: number | null;
  platform_support: number;
  summary: string;
}

export interface InsightsComputedFrom {
  window_start: ISODateTime;
  window_end: ISODateTime;
  baseline_start: ISODateTime;
  baseline_end: ISODateTime;
  segment?: Record<string, string>;
  window_payments: number;
  window_failures: number;
  baseline_payments: number;
  baseline_failures: number;
}

export interface IncidentInsights {
  outliers?: InsightsOutlier[];
  platform_callout?: PlatformCallout | null;
  computed_from: InsightsComputedFrom;
}

export interface IncidentDetail extends IncidentSummary {
  description?: string | null;
  window_start?: ISODateTime | null;
  window_end?: ISODateTime | null;
  resolved_at?: ISODateTime | null;
  root_cause?: string | null;
  timeline?: IncidentTimelineEvent[];
  evidence?: EvidenceItem[];
  diagnosis?: DiagnosisView | null;
  segment?: Record<string, string>;
  simulator_run_id?: string | null;
  opportunities_count?: number;
  recovery_actions_count?: number;
  revenue?: RevenueBreakdown | null;
  insights?: IncidentInsights | null;
}

export interface InvestigateRequest {
  force_refresh?: boolean;
}

// ---------------------------------------------------------------------------
// Recovery
// ---------------------------------------------------------------------------

export interface OpportunitySummary {
  id: string;
  incident_id?: string | null;
  payment_id?: string | null;
  customer_id?: string | null;
  subscription_id?: string | null;
  opportunity_type: string;
  status: RecoveryStatus;
  amount_paise: number;
  currency: string;
  expected_recovery_paise: number;
  confidence: number;
  risk: string;
  reason?: string | null;
  created_at: ISODateTime;
  expires_at?: ISODateTime | null;
}

export interface OpportunityListResponse {
  items: OpportunitySummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface PolicyDecisionView {
  id: string;
  outcome: PolicyOutcome;
  reasons: string[];
  rules_matched: string[];
  policy_version: string;
  decided_at: ISODateTime;
}

/** One append-only audit trail row referenced by a recovery resource. */
export interface AuditRef {
  id: string;
  actor: string;
  action: string;
  entity_type: string;
  entity_id: string;
  request_id?: string | null;
  details?: Record<string, unknown>;
  created_at: ISODateTime;
}

export interface RecoveryActionView {
  id: string;
  opportunity_id: string;
  strategy_id?: string | null;
  action_type: ActionType;
  status: RecoveryStatus;
  amount_paise: number;
  currency: string;
  confidence: number;
  actor: string;
  attempts: number;
  gateway_request_id?: string | null;
  policy_decision?: PolicyDecisionView | null;
  proposed_at: ISODateTime;
  executed_at?: ISODateTime | null;
  verified_at?: ISODateTime | null;
  completed_at?: ISODateTime | null;
  approved_by?: string | null;
  note?: string | null;
  last_error?: string | null;
}

export interface OpportunityDetail extends OpportunitySummary {
  constraints?: Record<string, unknown>;
  actions?: RecoveryActionView[];
  audit?: AuditRef[];
}

export interface StrategyOption {
  id: string;
  action_type: ActionType;
  rank: number;
  expected_recovery_paise: number;
  confidence: number;
  risk: string;
  eligibility: boolean;
  reason?: string | null;
  constraints?: Record<string, unknown>;
  generated_by: string;
  selected: boolean;
}

export interface PolicyPreview {
  outcome: PolicyOutcome;
  reasons: string[];
}

export interface RecoveryPlan {
  opportunity_id: string;
  strategies: StrategyOption[];
  recommended_strategy_id?: string | null;
  policy_preview?: PolicyPreview | null;
}

export interface ActionResponse {
  action_id?: string | null;
  opportunity_id: string;
  status: RecoveryStatus;
  message: string;
  policy_decision?: PolicyDecisionView | null;
}

export interface ApproveRequest {
  actor?: string;
  note?: string | null;
}

export interface RejectRequest {
  actor?: string;
  reason: string;
}

export interface EscalateRequest {
  actor?: string;
  reason: string;
}

export interface ExecuteRequest {
  strategy_id?: string | null;
  actor?: string;
}

export interface CancelRequest {
  actor?: string;
  reason?: string | null;
}

// ---------------------------------------------------------------------------
// Audit
// ---------------------------------------------------------------------------

export interface AuditLogEntry {
  id: string;
  entity_type: string;
  entity_id: string;
  actor: string;
  action: string;
  details?: Record<string, unknown>;
  request_id?: string | null;
  created_at: ISODateTime;
}

export interface AuditListResponse {
  items: AuditLogEntry[];
  total: number;
  page: number;
  page_size: number;
}

// ---------------------------------------------------------------------------
// Evaluation
// ---------------------------------------------------------------------------

export interface EvaluationMetrics {
  runs_count: number;
  detection_precision?: number | null;
  detection_recall?: number | null;
  detection_f1?: number | null;
  diagnosis_top1_accuracy?: number | null;
  mean_time_to_detect_minutes?: number | null;
  mean_time_to_recover_minutes?: number | null;
  recovery_rate?: number | null;
  recovered_revenue_paise: number;
  false_action_rate?: number | null;
  currency: string;
  diagnosis_top3_accuracy?: number | null;
  unsafe_action_count?: number;
  baseline_recovery_rate?: number | null;
  baseline_recovered_revenue_paise?: number;
  /** Mean incremental lift (treatment − holdout) over completed runs that
   * carried a randomized holdout; null/absent when no run has one. */
  incremental_lift?: number | null;
}

export interface EvaluationRunSummary {
  id: string;
  name: string;
  evaluation_type: string;
  dataset: string;
  simulator_run_id?: string | null;
  status: string;
  started_at?: ISODateTime | null;
  finished_at?: ISODateTime | null;
}

export interface EvaluationRunListResponse {
  items: EvaluationRunSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface EvaluationRunDetail extends EvaluationRunSummary {
  metrics: Record<string, unknown>;
  notes?: string | null;
}

export interface RunEvaluationRequest {
  name?: string;
  evaluation_type?: string;
  dataset?: string;
  simulator_run_id?: string | null;
  /** Scenario preset name; overrides `dataset`. */
  scenario?: string | null;
  /** Scale knobs; omitted values use the scenario preset's own scale. */
  seed?: number | null;
  days?: number | null;
  events?: number | null;
  customers?: number | null;
  /** Share of customers randomized into the no-action holdout inside the
   * PulseRecover arm; omitted -> harness default (0.10), 0 disables. */
  holdout_fraction?: number | null;
}

export interface RunEvaluationResponse {
  run_id: string;
  status: string;
  started_at: ISODateTime;
  finished_at?: ISODateTime | null;
  experiment_id?: string | null;
  metrics?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Detection / demo
// ---------------------------------------------------------------------------

export interface DetectionRunRequest {
  window_minutes?: number;
  metrics?: string[] | null;
  dry_run?: boolean;
}

export interface ScenarioInfo {
  name: string;
  description: string;
  expected_incident_metric?: string | null;
}

export interface ScenarioListResponse {
  scenarios: ScenarioInfo[];
}

export interface DemoResetResponse {
  status: string;
  cleared: Record<string, number>;
  reset_at?: ISODateTime | null;
  /** Tables deliberately preserved (the scientific record). */
  kept?: string[];
  audit_id?: string | null;
}

export interface ScenarioTriggerResponse {
  scenario: string;
  status: string;
  simulator_run_id?: string | null;
  incident_id?: string | null;
  detail?: string | null;
  skipped?: boolean;
  stats?: Record<string, unknown>;
  detection?: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// Query parameter shapes
// ---------------------------------------------------------------------------

export type QueryValue = string | number | boolean | null | undefined;

export interface PageParams {
  page?: number;
  page_size?: number;
}

export interface IncidentListParams extends PageParams {
  status?: IncidentStatus | null;
  severity?: Severity | null;
  metric?: string | null;
  detected_from?: ISODateTime | null;
  detected_to?: ISODateTime | null;
}

export interface OpportunityListParams extends PageParams {
  status?: RecoveryStatus | null;
  incident_id?: string | null;
  opportunity_type?: string | null;
  customer_id?: string | null;
}

export interface AuditListParams extends PageParams {
  entity_type?: string | null;
  entity_id?: string | null;
}

export interface TimeseriesParams {
  metric?: string;
  granularity?: Granularity;
  window_hours?: number;
}

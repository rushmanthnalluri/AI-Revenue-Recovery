import type { JsonObject } from "@/lib/types";

/**
 * Defensive readers for the evaluation run `metrics` JSON column.
 *
 * The backend (app/services/evaluation/runner.py) assembles the payload as
 * `{ arms: { baseline, pulsecover }, comparison, ...flat aggregates }`; the
 * typed contract exposes it as `Record<string, unknown>`, so every accessor
 * here validates before reading. Nothing is computed — values are served
 * exactly as stored, only narrowed and renamed for display.
 */

export interface EvalDetection {
  passes?: number;
  incidents?: number;
  matchedIncidents?: number;
  matchedGroundTruth?: number;
  precision?: number | null;
  recall?: number | null;
  f1?: number | null;
  mttdMinutes?: number | null;
}

export interface EvalPerIncident {
  incidentId: string;
  truth: string;
  predicted?: string;
  confidence?: number;
  top3: string[];
  correct?: boolean;
  error?: string;
}

export interface EvalDiagnosis {
  scoredIncidents?: number;
  top1Accuracy?: number | null;
  top3Accuracy?: number | null;
  perIncident: EvalPerIncident[];
}

export interface EvalArm {
  simulatorRunId?: string;
  failedPaymentsCount?: number;
  failedAmountPaise?: number;
  interventionsCount?: number;
  ungatedActionsCount?: number;
  falseInterventionsCount?: number;
  falseInterventionAmountPaise?: number;
  recoveredRevenuePaise?: number;
  recoveryRate?: number;
  /* pulsecover arm only */
  groundTruthIncidents?: number;
  opportunitiesCount?: number;
  actionsCount?: number;
  approvalsRequired?: number;
  recoveredActionsCount?: number;
  unknownActionsCount?: number;
  unsafeActionCount?: number;
  falseActionRate?: number | null;
  mttrMinutes?: number | null;
  detection?: EvalDetection;
  diagnosis?: EvalDiagnosis;
  policyOutcomes?: Record<string, number>;
  interventionsByType?: Record<string, number>;
}

export interface EvalComparison {
  recoveredRevenueDeltaPaise?: number;
  recoveredRevenueRatio?: number | null;
  recoveryRateDelta?: number;
  interventionsBaseline?: number;
  interventionsPulsecover?: number;
  interventionReduction?: number | null;
  falseInterventionsBaseline?: number;
  falseInterventionsPulsecover?: number;
}

export interface EvalHoldoutGroup {
  failedPayments?: number;
  failedAmountPaise?: number;
  recoveredPayments?: number;
  recoveredAmountPaise?: number;
  recoveryRate?: number;
  recoveryRateAmount?: number;
  medianTimeToRecoverMinutes?: number | null;
  /* treatment group only */
  recoveredViaAction?: number;
  recoveredOrganic?: number;
}

export interface EvalLift {
  point?: number;
  ci95Low?: number;
  ci95High?: number;
}

export interface EvalLiftStratum {
  stratum: string;
  treatment: EvalHoldoutGroup;
  holdout: EvalHoldoutGroup;
  lift: EvalLift;
}

export interface EvalHoldout {
  configuredFraction?: number;
  realizedFraction?: number;
  seed?: number;
  assignment?: string;
  estimand?: string;
  attributionWindowHours?: number | null;
  ciMethod?: string;
  customersTreatment?: number;
  customersHoldout?: number;
  treatment: EvalHoldoutGroup;
  holdout: EvalHoldoutGroup;
  lift: EvalLift;
  /** Mix-adjusted (class-standardized) secondary estimator. */
  liftAdjusted?: EvalLift;
  strataByFailureClass: EvalLiftStratum[];
  strataByMethod: EvalLiftStratum[];
  holdoutOpportunitiesCount?: number;
  holdoutActionsCount?: number;
  notes: string[];
}

export interface EvalOutcomeModel {
  provenance?: string;
  assumptions: string[];
}

export interface EvalRunProvenance {
  seed?: number;
  datasetVersion?: string;
  anchor?: string;
  diagnosisArtifact?: string;
  policyVersion?: string;
}

export interface ParsedRunMetrics {
  baseline: EvalArm | null;
  pulsecover: EvalArm | null;
  comparison: EvalComparison | null;
  holdout: EvalHoldout | null;
  outcomeModel: EvalOutcomeModel | null;
}

function asRecord(value: unknown): JsonObject | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function asNumberOrNull(value: unknown): number | null | undefined {
  if (value === null) return null;
  return asNumber(value);
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function asNumberRecord(value: unknown): Record<string, number> | undefined {
  const rec = asRecord(value);
  if (!rec) return undefined;
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(rec)) {
    const n = asNumber(v);
    if (n !== undefined) out[k] = n;
  }
  return out;
}

function parseDetection(value: unknown): EvalDetection | undefined {
  const rec = asRecord(value);
  if (!rec) return undefined;
  return {
    passes: asNumber(rec.passes),
    incidents: asNumber(rec.incidents),
    matchedIncidents: asNumber(rec.matched_incidents),
    matchedGroundTruth: asNumber(rec.matched_ground_truth),
    precision: asNumberOrNull(rec.precision),
    recall: asNumberOrNull(rec.recall),
    f1: asNumberOrNull(rec.f1),
    mttdMinutes: asNumberOrNull(rec.mttd_minutes),
  };
}

function parsePerIncident(value: unknown): EvalPerIncident[] {
  if (!Array.isArray(value)) return [];
  const out: EvalPerIncident[] = [];
  for (const item of value) {
    const rec = asRecord(item);
    if (!rec) continue;
    const incidentId = asString(rec.incident_id);
    const truth = asString(rec.truth);
    if (!incidentId || !truth) continue;
    out.push({
      incidentId,
      truth,
      predicted: asString(rec.predicted),
      confidence: asNumber(rec.confidence),
      top3: asStringArray(rec.top3),
      correct: typeof rec.correct === "boolean" ? rec.correct : undefined,
      error: asString(rec.error),
    });
  }
  return out;
}

function parseDiagnosis(value: unknown): EvalDiagnosis | undefined {
  const rec = asRecord(value);
  if (!rec) return undefined;
  return {
    scoredIncidents: asNumber(rec.scored_incidents),
    top1Accuracy: asNumberOrNull(rec.top1_accuracy),
    top3Accuracy: asNumberOrNull(rec.top3_accuracy),
    perIncident: parsePerIncident(rec.per_incident),
  };
}

function parseArm(value: unknown): EvalArm | null {
  const rec = asRecord(value);
  if (!rec) return null;
  return {
    simulatorRunId: asString(rec.simulator_run_id),
    failedPaymentsCount: asNumber(rec.failed_payments_count),
    failedAmountPaise: asNumber(rec.failed_amount_paise),
    interventionsCount: asNumber(rec.interventions_count),
    ungatedActionsCount: asNumber(rec.ungated_actions_count),
    falseInterventionsCount: asNumber(rec.false_interventions_count),
    falseInterventionAmountPaise: asNumber(rec.false_intervention_amount_paise),
    recoveredRevenuePaise: asNumber(rec.recovered_revenue_paise),
    recoveryRate: asNumber(rec.recovery_rate),
    groundTruthIncidents: asNumber(rec.ground_truth_incidents),
    opportunitiesCount: asNumber(rec.opportunities_count),
    actionsCount: asNumber(rec.actions_count),
    approvalsRequired: asNumber(rec.approvals_required),
    recoveredActionsCount: asNumber(rec.recovered_actions_count),
    unknownActionsCount: asNumber(rec.unknown_actions_count),
    unsafeActionCount: asNumber(rec.unsafe_action_count),
    falseActionRate: asNumberOrNull(rec.false_action_rate),
    mttrMinutes: asNumberOrNull(rec.mttr_minutes),
    detection: parseDetection(rec.detection),
    diagnosis: parseDiagnosis(rec.diagnosis),
    policyOutcomes: asNumberRecord(rec.policy_outcomes),
    interventionsByType: asNumberRecord(rec.interventions_by_type),
  };
}

function parseComparison(value: unknown): EvalComparison | null {
  const rec = asRecord(value);
  if (!rec) return null;
  return {
    recoveredRevenueDeltaPaise: asNumber(rec.recovered_revenue_delta_paise),
    recoveredRevenueRatio: asNumberOrNull(rec.recovered_revenue_ratio),
    recoveryRateDelta: asNumber(rec.recovery_rate_delta),
    interventionsBaseline: asNumber(rec.interventions_baseline),
    interventionsPulsecover: asNumber(rec.interventions_pulserecover),
    interventionReduction: asNumberOrNull(rec.intervention_reduction),
    falseInterventionsBaseline: asNumber(rec.false_interventions_baseline),
    falseInterventionsPulsecover: asNumber(rec.false_interventions_pulserecover),
  };
}

function parseHoldoutGroup(value: unknown): EvalHoldoutGroup {
  const rec = asRecord(value);
  if (!rec) return {};
  return {
    failedPayments: asNumber(rec.failed_payments),
    failedAmountPaise: asNumber(rec.failed_amount_paise),
    recoveredPayments: asNumber(rec.recovered_payments),
    recoveredAmountPaise: asNumber(rec.recovered_amount_paise),
    recoveryRate: asNumber(rec.recovery_rate),
    recoveryRateAmount: asNumber(rec.recovery_rate_amount),
    medianTimeToRecoverMinutes: asNumberOrNull(rec.median_time_to_recover_minutes),
    recoveredViaAction: asNumber(rec.recovered_via_action),
    recoveredOrganic: asNumber(rec.recovered_organic),
  };
}

function parseLift(value: unknown): EvalLift {
  const rec = asRecord(value);
  if (!rec) return {};
  return {
    point: asNumber(rec.point),
    ci95Low: asNumber(rec.ci95_low),
    ci95High: asNumber(rec.ci95_high),
  };
}

function parseLiftStrata(value: unknown): EvalLiftStratum[] {
  if (!Array.isArray(value)) return [];
  const out: EvalLiftStratum[] = [];
  for (const item of value) {
    const rec = asRecord(item);
    const stratum = rec ? asString(rec.stratum) : undefined;
    if (!rec || !stratum) continue;
    out.push({
      stratum,
      treatment: parseHoldoutGroup(rec.treatment),
      holdout: parseHoldoutGroup(rec.holdout),
      lift: parseLift(rec.lift),
    });
  }
  return out;
}

/**
 * The randomized-holdout section. Returns null unless the core objects are
 * present — runs stored before the holdout arm existed (or with fraction 0)
 * simply have no section, and the UI hides it cleanly.
 */
function parseHoldout(value: unknown): EvalHoldout | null {
  const rec = asRecord(value);
  if (!rec) return null;
  const treatment = asRecord(rec.treatment);
  const holdout = asRecord(rec.holdout);
  const lift = asRecord(rec.lift);
  if (!treatment || !holdout || !lift) return null;
  const attribution = asRecord(rec.attribution_window);
  const customers = asRecord(rec.customers);
  const isolation = asRecord(rec.isolation);
  const strata = asRecord(rec.strata);
  return {
    configuredFraction: asNumber(rec.configured_fraction),
    realizedFraction: asNumber(rec.realized_fraction),
    seed: asNumber(rec.seed),
    assignment: asString(rec.assignment),
    estimand: asString(rec.estimand),
    attributionWindowHours: asNumberOrNull(attribution?.max_window_hours),
    ciMethod: asString(rec.ci_method),
    customersTreatment: asNumber(customers?.treatment),
    customersHoldout: asNumber(customers?.holdout),
    treatment: parseHoldoutGroup(treatment),
    holdout: parseHoldoutGroup(holdout),
    lift: parseLift(lift),
    liftAdjusted: parseLift(rec.lift_class_adjusted),
    strataByFailureClass: parseLiftStrata(strata?.by_failure_class),
    strataByMethod: parseLiftStrata(strata?.by_method),
    holdoutOpportunitiesCount: asNumber(isolation?.holdout_opportunities_count),
    holdoutActionsCount: asNumber(isolation?.holdout_actions_count),
    notes: asStringArray(rec.notes),
  };
}

/** Narrow the stored metrics JSON into typed arm / comparison views. */
export function parseRunMetrics(metrics: JsonObject): ParsedRunMetrics {
  const arms = asRecord(metrics.arms);
  return {
    baseline: parseArm(arms?.baseline),
    pulsecover: parseArm(arms?.pulsecover),
    comparison: parseComparison(metrics.comparison),
    holdout: parseHoldout(metrics.holdout),
    outcomeModel: parseOutcomeModel(metrics.outcome_model),
  };
}

function parseOutcomeModel(value: unknown): EvalOutcomeModel | null {
  const rec = asRecord(value);
  if (!rec) return null;
  return {
    provenance: asString(rec.provenance),
    assumptions: asStringArray(rec.assumptions),
  };
}

/**
 * Run-record completeness fields (runner.py writes `metrics.dataset` /
 * `metrics.versions` on completion; older runs simply lack the keys, and
 * every field degrades to absent rather than to a placeholder).
 */
export function parseRunProvenance(metrics: JsonObject): EvalRunProvenance {
  const dataset = asRecord(metrics.dataset);
  const versions = asRecord(metrics.versions);
  return {
    seed: asNumber(dataset?.seed),
    datasetVersion: asString(dataset?.dataset_version),
    anchor: asString(dataset?.anchor),
    diagnosisArtifact: asString(versions?.diagnosis_artifact),
    policyVersion: asString(versions?.policy),
  };
}

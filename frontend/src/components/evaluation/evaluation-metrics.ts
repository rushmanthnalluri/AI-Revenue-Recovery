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

export interface ParsedRunMetrics {
  baseline: EvalArm | null;
  pulsecover: EvalArm | null;
  comparison: EvalComparison | null;
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

/** Narrow the stored metrics JSON into typed arm / comparison views. */
export function parseRunMetrics(metrics: JsonObject): ParsedRunMetrics {
  const arms = asRecord(metrics.arms);
  return {
    baseline: parseArm(arms?.baseline),
    pulsecover: parseArm(arms?.pulsecover),
    comparison: parseComparison(metrics.comparison),
  };
}

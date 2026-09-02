"use client";

import * as React from "react";
import { CircleAlert, Loader2 } from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { SectionCard } from "@/components/section-card";
import { StatusPill } from "@/components/status-pill";
import { EvaluationComparison } from "@/components/evaluation/evaluation-comparison";
import { EvaluationHoldout } from "@/components/evaluation/evaluation-holdout";
import { EvaluationMethodology } from "@/components/evaluation/evaluation-methodology";
import { EvaluationMetricBars } from "@/components/evaluation/evaluation-metric-bars";
import {
  parseRunMetrics,
  parseRunProvenance,
  type ParsedRunMetrics,
} from "@/components/evaluation/evaluation-metrics";
import { EvaluationOperational } from "@/components/evaluation/evaluation-operational";
import { EvaluationPerIncident } from "@/components/evaluation/evaluation-per-incident";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime, formatMinutes, formatPercent } from "@/lib/format";
import type { EvaluationRunDetail } from "@/lib/types";

function durationLabel(run: EvaluationRunDetail): string {
  if (!run.started_at || !run.finished_at) return "—";
  const ms = new Date(run.finished_at).getTime() - new Date(run.started_at).getTime();
  if (Number.isNaN(ms) || ms < 0) return "—";
  return formatMinutes(ms / 60_000);
}

function HeaderFacts({ run }: { run: EvaluationRunDetail }) {
  const provenance = parseRunProvenance(run.metrics);
  const facts: [string, string][] = [
    ["run id", run.id],
    ["scenario", run.dataset],
    ["type", run.evaluation_type],
    ["started", formatDateTime(run.started_at)],
    ["finished", formatDateTime(run.finished_at)],
    ["duration", durationLabel(run)],
  ];
  // Run-record completeness (runner.py writes metrics.dataset / metrics.versions
  // on completion) — surfaced compactly, only the fields the row actually has.
  if (provenance.seed !== undefined) facts.push(["seed", String(provenance.seed)]);
  if (provenance.datasetVersion) facts.push(["dataset version", provenance.datasetVersion]);
  if (provenance.anchor) facts.push(["anchor", formatDateTime(provenance.anchor)]);
  if (provenance.diagnosisArtifact) facts.push(["diagnosis model", provenance.diagnosisArtifact]);
  if (provenance.policyVersion) facts.push(["policy version", provenance.policyVersion]);
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-3">
      {facts.map(([label, value]) => (
        <div key={label} className="min-w-0">
          <dt className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
            {label}
          </dt>
          <dd className="mt-0.5 break-words font-mono text-xs tabular-nums text-text-2">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function RunningState() {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2.5 rounded-lg border border-accent-border bg-accent-wash px-4 py-3.5 text-[13.5px] text-text-2">
        <Loader2 className="size-4 animate-spin text-accent" strokeWidth={1.5} aria-hidden />
        Run executing on the server — two simulator arms, then scoring. Metrics appear here
        the moment the stored row completes; this page polls automatically.
      </div>
      <div className="space-y-2" aria-busy="true" aria-label="Run in progress">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    </div>
  );
}

function FailedState({ run }: { run: EvaluationRunDetail }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-[rgba(198,93,85,0.45)] bg-danger-dim px-4 py-3.5"
    >
      <div className="flex items-center gap-2 text-danger">
        <CircleAlert className="size-4" strokeWidth={1.5} aria-hidden />
        <p className="text-[13.5px] font-semibold">Evaluation run failed</p>
      </div>
      <p className="mt-1 text-[13px] text-text-2">
        The harness persisted the failure honestly — the stored exception is in the run notes.
      </p>
      {run.notes ? (
        <pre className="mt-2 max-h-40 overflow-auto rounded-md border border-border bg-bg p-3 font-mono text-[11px] text-text-2">
          {run.notes}
        </pre>
      ) : null}
    </div>
  );
}

function CompletedSections({ run, parsed }: { run: EvaluationRunDetail; parsed: ParsedRunMetrics }) {
  const { baseline, pulsecover, comparison, holdout, outcomeModel } = parsed;
  const detection = pulsecover?.detection;
  const diagnosis = pulsecover?.diagnosis;

  const detectionData = detection
    ? [
        { key: "precision", label: "Precision", pulsecover: detection.precision },
        { key: "recall", label: "Recall", pulsecover: detection.recall },
        { key: "f1", label: "F1", pulsecover: detection.f1 },
      ]
    : [];

  const diagnosisData = diagnosis
    ? [
        { key: "top1", label: "Top-1", pulsecover: diagnosis.top1Accuracy },
        { key: "top3", label: "Top-3", pulsecover: diagnosis.top3Accuracy },
      ]
    : [];

  const recoveryData =
    baseline && pulsecover
      ? [
          {
            key: "recovery-rate",
            label: "Recovery rate",
            baseline: baseline.recoveryRate,
            pulsecover: pulsecover.recoveryRate,
          },
        ]
      : [];

  const interventionData =
    baseline && pulsecover
      ? [
          {
            key: "interventions",
            label: "Interventions",
            baseline: baseline.interventionsCount,
            pulsecover: pulsecover.interventionsCount,
          },
          {
            key: "false",
            label: "False interventions",
            baseline: baseline.falseInterventionsCount,
            pulsecover: pulsecover.falseInterventionsCount,
          },
        ]
      : [];

  return (
    <>
      {baseline && pulsecover ? (
        <SectionCard
          title="Baseline vs PulseRecover"
          description="Stored arm metrics — the naive retry-everything default against the full gated loop"
        >
          <EvaluationComparison baseline={baseline} pulsecover={pulsecover} comparison={comparison} />
        </SectionCard>
      ) : (
        <EmptyState
          title="No arm comparison in this run"
          description="The stored metrics payload carries no arms.baseline / arms.pulsecover objects — only completed end-to-end runs produce them."
        />
      )}

      {baseline && pulsecover ? (
        <SectionCard
          title="Operational outcomes — measured, not incremental"
          description="Action-level facts from the stored run: the executed actions worked and were safe. By itself this says nothing about fleet-level causal lift — that estimate, with its uncertainty, is the holdout section below."
        >
          <EvaluationOperational
            baseline={baseline}
            pulsecover={pulsecover}
            comparison={comparison}
            holdout={holdout}
          />
        </SectionCard>
      ) : null}

      {holdout ? (
        <SectionCard
          title="Incremental lift (holdout-adjusted)"
          description="Randomized customer holdout — treatment vs no-action recovery over the run's fixed attribution window, with 95% confidence intervals"
        >
          <EvaluationHoldout holdout={holdout} />
        </SectionCard>
      ) : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <SectionCard
          title="Detection quality"
          description="Vs simulator ground truth — scheduled passes, organic noise included"
          actions={
            detection?.mttdMinutes !== undefined && detection.mttdMinutes !== null ? (
              <Badge variant="accent">MTTD {formatMinutes(detection.mttdMinutes)}</Badge>
            ) : undefined
          }
        >
          {detectionData.length > 0 ? (
            <EvaluationMetricBars
              data={detectionData}
              format="percent"
              ariaLabel={`Detection precision ${formatPercent(detection?.precision)}, recall ${formatPercent(detection?.recall)}, F1 ${formatPercent(detection?.f1)}. The baseline arm has no detection stage.`}
            />
          ) : (
            <EmptyState
              title="No detection metrics stored"
              description="This run's payload has no arms.pulsecover.detection object."
            />
          )}
          {detection ? (
            <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
              {detection.matchedGroundTruth ?? "—"} of{" "}
              {pulsecover?.groundTruthIncidents ?? "—"} ground-truth incidents matched · baseline
              arm has no detection stage
            </p>
          ) : null}
        </SectionCard>

        <SectionCard
          title="Diagnosis accuracy"
          description="Root-cause classifier vs injected ground truth"
          actions={
            pulsecover?.mttrMinutes !== undefined && pulsecover.mttrMinutes !== null ? (
              <Badge variant="accent">MTTR {formatMinutes(pulsecover.mttrMinutes)}</Badge>
            ) : undefined
          }
        >
          {diagnosisData.length > 0 ? (
            <EvaluationMetricBars
              data={diagnosisData}
              format="percent"
              ariaLabel={`Diagnosis top-1 accuracy ${formatPercent(diagnosis?.top1Accuracy)}, top-3 accuracy ${formatPercent(diagnosis?.top3Accuracy)} over ${diagnosis?.scoredIncidents ?? 0} scored incidents.`}
            />
          ) : (
            <EmptyState
              title="No diagnosis metrics stored"
              description="This run's payload has no arms.pulsecover.diagnosis object."
            />
          )}
          {diagnosis?.scoredIncidents !== undefined ? (
            <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
              {diagnosis.scoredIncidents} incidents scored · first detection per ground-truth
              incident
            </p>
          ) : null}
        </SectionCard>

        <SectionCard
          title="Recovery rate"
          description="Recovered ÷ failed amount, per arm — gross for the baseline, verified for PulseRecover"
        >
          {recoveryData.length > 0 ? (
            <EvaluationMetricBars
              data={recoveryData}
              format="percent"
              ariaLabel={`Recovery rate — baseline ${formatPercent(baseline?.recoveryRate)}, PulseRecover ${formatPercent(pulsecover?.recoveryRate)}.`}
            />
          ) : (
            <EmptyState title="No recovery metrics stored" />
          )}
        </SectionCard>

        <SectionCard
          title="Intervention cost"
          description="Customer-facing actions fired — and how many should never have happened"
        >
          {interventionData.length > 0 ? (
            <EvaluationMetricBars
              data={interventionData}
              format="number"
              ariaLabel={`Interventions — baseline ${baseline?.interventionsCount ?? "—"}, PulseRecover ${pulsecover?.interventionsCount ?? "—"}. False interventions — baseline ${baseline?.falseInterventionsCount ?? "—"}, PulseRecover ${pulsecover?.falseInterventionsCount ?? "—"}.`}
            />
          ) : (
            <EmptyState title="No intervention metrics stored" />
          )}
        </SectionCard>
      </div>

      {diagnosis && diagnosis.perIncident.length > 0 ? (
        <SectionCard
          title="Per-incident diagnosis"
          description="Every scored incident — truth, prediction, confidence"
          contentClassName="pt-0"
        >
          <EvaluationPerIncident rows={diagnosis.perIncident} />
        </SectionCard>
      ) : null}

      <SectionCard
        title="Methodology & honest caveats"
        description="What these numbers mean — and where they are weak (docs/evaluation.md)"
      >
        <EvaluationMethodology notes={run.notes} assumptions={outcomeModel?.assumptions} />
      </SectionCard>
    </>
  );
}

/** Full detail stack for the selected evaluation run, by stored status. */
export function EvaluationRunDetailView({ run }: { run: EvaluationRunDetail }) {
  return (
    <div className="space-y-4">
      <SectionCard
        title={run.name}
        description="Stored run row — the console never recomputes metrics"
        actions={<StatusPill status={run.status} pulse={run.status === "running"} />}
      >
        <HeaderFacts run={run} />
      </SectionCard>

      {run.status === "running" ? (
        <RunningState />
      ) : run.status === "failed" ? (
        <FailedState run={run} />
      ) : (
        <CompletedSections run={run} parsed={parseRunMetrics(run.metrics)} />
      )}
    </div>
  );
}

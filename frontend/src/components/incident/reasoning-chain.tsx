import * as React from "react";
import Link from "next/link";

import type { IncidentDetail } from "@/lib/types";
import { formatDateTime, formatINR, formatPercent } from "@/lib/format";
import {
  formatDeviation,
  formatMetricValue,
  metricLabel,
} from "@/components/incident/incident-metric";
import { actionTypeLabel } from "@/components/recovery/recovery-contract";
import { SectionCard } from "@/components/section-card";
import { StatusPill } from "@/components/status-pill";
import { cn } from "@/lib/utils";

type StepState = "done" | "pending";

interface ChainStep {
  key: string;
  label: string;
  state: StepState;
  body: React.ReactNode;
}

function StepRow({ step, last }: { step: ChainStep; last: boolean }) {
  return (
    <li className={cn("flex gap-4 py-3", !last && "border-b border-border")}>
      <span className="w-32 shrink-0 pt-0.5 font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
        {step.label}
      </span>
      <div className="min-w-0 flex-1 text-[13px] leading-relaxed text-text-2">{step.body}</div>
      <span
        className={cn(
          "shrink-0 self-start rounded-sm border px-[7px] py-[3px] font-mono text-[9.5px] uppercase tracking-[0.07em]",
          step.state === "done"
            ? "border-transparent bg-success-dim text-success"
            : "border-border-strong text-text-3",
        )}
      >
        {step.state === "done" ? "recorded" : "pending"}
      </span>
    </li>
  );
}

/**
 * Reasoning chain — the incident's signal → diagnosis → revenue-at-risk →
 * recovery → outcome narrative in one compact read. Every line is derived
 * from typed IncidentDetail fields (no chain-of-thought, no generated prose);
 * steps whose data has not been produced yet render as "pending" rather than
 * being omitted or fabricated.
 */
export function ReasoningChain({ incident }: { incident: IncidentDetail }) {
  const diagnosis = incident.diagnosis ?? null;
  const evidenceCount = incident.evidence?.length ?? 0;
  const loss = incident.revenue?.observed_loss ?? null;
  const recoverable = incident.revenue?.recoverable ?? null;
  const strategyExpectations = Object.entries(
    incident.revenue?.expected_recovery_by_strategy ?? {},
  );
  const opportunities = incident.opportunities_count ?? 0;
  const actions = incident.recovery_actions_count ?? 0;

  const steps: ChainStep[] = [
    {
      key: "signal",
      label: "Signal",
      state: "done",
      body: (
        <>
          {metricLabel(incident.metric)} observed at{" "}
          <span className="font-mono text-text">
            {formatMetricValue(incident.metric, incident.observed_value)}
          </span>{" "}
          vs baseline{" "}
          <span className="font-mono text-text">
            {formatMetricValue(incident.metric, incident.baseline_value)}
          </span>{" "}
          — deviation{" "}
          <span className="font-mono text-text">{formatDeviation(incident.deviation_pct)}</span>,{" "}
          detected {formatDateTime(incident.detected_at)} by{" "}
          <span className="font-mono">{incident.detection_method.replace(/_/g, " ")}</span>.
        </>
      ),
    },
    {
      key: "diagnosis",
      label: "Diagnosis",
      state: diagnosis ? "done" : "pending",
      body: diagnosis ? (
        <>
          <span className="text-text">{diagnosis.predicted_cause.replace(/_/g, " ")}</span> —{" "}
          {formatPercent(diagnosis.confidence, 0)} confidence from{" "}
          <span className="font-mono">
            {diagnosis.model_name}@{diagnosis.model_version}
          </span>
          {evidenceCount > 0
            ? `, backed by ${evidenceCount} collected evidence item${evidenceCount === 1 ? "" : "s"}`
            : ""}
          .
        </>
      ) : (
        "Pending — the diagnosis model runs automatically on first view; it may still be running."
      ),
    },
    {
      key: "risk",
      label: "Revenue at risk",
      state: loss && loss.point_paise !== null && loss.point_paise !== undefined ? "done" : "pending",
      body:
        loss && loss.point_paise !== null && loss.point_paise !== undefined ? (
          <>
            <span className="font-mono text-text">{formatINR(loss.point_paise)}</span> estimated
            loss (CI {formatINR(loss.lower_paise)}–{formatINR(loss.upper_paise)})
            {loss.low_confidence ? " — low-confidence point estimate" : ""}
            {recoverable?.point_paise !== null && recoverable?.point_paise !== undefined ? (
              <>
                ;{" "}
                <span className="font-mono text-text">
                  {formatINR(recoverable.point_paise)}
                </span>{" "}
                of it estimated recoverable
              </>
            ) : null}
            .{loss.basis ? <span className="block text-xs text-text-3">Basis: {loss.basis}.</span> : null}
          </>
        ) : (
          "No estimate yet — the revenue engine could not derive a counterfactual loss from the available baseline."
        ),
    },
    {
      key: "recovery",
      label: "Recovery",
      state: opportunities > 0 ? "done" : "pending",
      body:
        opportunities > 0 ? (
          <>
            {opportunities} {opportunities === 1 ? "opportunity" : "opportunities"} built ·{" "}
            {actions} recovery {actions === 1 ? "action" : "actions"} — policy-gated in the{" "}
            <Link href="/recovery" className="text-accent underline-offset-2 hover:underline">
              Recovery console
            </Link>
            .
            {strategyExpectations.length > 0 ? (
              <span className="block text-xs text-text-3">
                Expected recovery by strategy:{" "}
                {strategyExpectations
                  .slice(0, 3)
                  .map(([strategy, estimate]) =>
                    estimate.point_paise !== null && estimate.point_paise !== undefined
                      ? `${actionTypeLabel(strategy)} ${formatINR(estimate.point_paise)}`
                      : null,
                  )
                  .filter(Boolean)
                  .join(" · ")}
                .
              </span>
            ) : null}
          </>
        ) : (
          "No recovery opportunities yet — they are built from this incident's failed payments and dropped checkouts (action above), then gated by the policy engine."
        ),
    },
    {
      key: "outcome",
      label: "Outcome",
      state: incident.status === "RESOLVED" || incident.status === "CLOSED" ? "done" : "pending",
      body: (
        <span className="flex flex-wrap items-center gap-2">
          <StatusPill status={incident.status} />
          {incident.resolved_at
            ? `Resolved ${formatDateTime(incident.resolved_at)}.`
            : "Still in progress — state transitions land in the audit timeline below as they happen."}
        </span>
      ),
    },
  ];

  return (
    <SectionCard
      title="Reasoning chain"
      description="Signal → diagnosis → revenue at risk → recovery → outcome. Every line is derived from this incident's stored fields — the evidence sits in the sections below."
    >
      <ol aria-label="Incident reasoning chain">
        {steps.map((step, i) => (
          <StepRow key={step.key} step={step} last={i === steps.length - 1} />
        ))}
      </ol>
    </SectionCard>
  );
}

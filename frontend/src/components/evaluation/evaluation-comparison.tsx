import * as React from "react";

import { CHART_PALETTE } from "@/components/chart-theme";
import type { EvalArm, EvalComparison } from "@/components/evaluation/evaluation-metrics";
import { formatDeltaPP, formatINR, formatNumber, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

interface EvaluationComparisonProps {
  baseline: EvalArm;
  pulsecover: EvalArm;
  comparison: EvalComparison | null;
}

function DeltaChip({ children, tone }: { children: React.ReactNode; tone: "success" | "danger" | "neutral" }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-sm border px-[7px] py-[2px] font-mono text-[10px] uppercase tracking-[0.07em]",
        tone === "success" && "border-transparent bg-success-dim text-success",
        tone === "danger" && "border-transparent bg-danger-dim text-danger",
        tone === "neutral" && "border-border-strong text-text-3",
      )}
    >
      {children}
    </span>
  );
}

interface CompareRow {
  key: string;
  label: string;
  baseline: React.ReactNode;
  pulsecover: React.ReactNode;
  pulsecoverTone?: "default" | "success" | "danger";
  delta?: React.ReactNode;
  hint?: string;
}

/**
 * Headline arm-vs-arm comparison — every cell is a stored value from the
 * run's metrics JSON (`arms.*` / `comparison.*`); nothing is recomputed.
 * Baseline is rendered in slate, PulseRecover in amber, matching the charts.
 */
export function EvaluationComparison({ baseline, pulsecover, comparison }: EvaluationComparisonProps) {
  const rows: CompareRow[] = [
    {
      key: "recovered",
      label: "Recovered revenue (verified)",
      baseline: formatINR(baseline.recoveredRevenuePaise ?? 0),
      pulsecover: formatINR(pulsecover.recoveredRevenuePaise ?? 0),
      delta:
        comparison?.recoveredRevenueDeltaPaise !== undefined ? (
          <DeltaChip tone={comparison.recoveredRevenueDeltaPaise >= 0 ? "success" : "neutral"}>
            {comparison.recoveredRevenueDeltaPaise >= 0 ? "+" : "−"}
            {formatINR(Math.abs(comparison.recoveredRevenueDeltaPaise), { compact: true })}
          </DeltaChip>
        ) : undefined,
    },
    {
      key: "rate",
      label: "Recovery rate (of failed amount)",
      baseline: formatPercent(baseline.recoveryRate),
      pulsecover: formatPercent(pulsecover.recoveryRate),
      delta:
        comparison?.recoveryRateDelta !== undefined ? (
          <DeltaChip tone="neutral">{formatDeltaPP(comparison.recoveryRateDelta)}</DeltaChip>
        ) : undefined,
    },
    {
      key: "interventions",
      label: "Interventions",
      baseline: formatNumber(baseline.interventionsCount),
      pulsecover: formatNumber(pulsecover.interventionsCount),
      delta:
        comparison?.interventionReduction !== undefined &&
        comparison.interventionReduction !== null ? (
          <DeltaChip tone="success">
            {formatPercent(comparison.interventionReduction)} fewer
          </DeltaChip>
        ) : undefined,
    },
    {
      key: "false",
      label: "False interventions (never-approve resubmissions)",
      baseline: formatNumber(baseline.falseInterventionsCount),
      pulsecover: formatNumber(pulsecover.falseInterventionsCount),
      pulsecoverTone:
        (pulsecover.falseInterventionsCount ?? 0) > 0 ? "default" : "success",
    },
    {
      key: "unsafe",
      label: "Ungated / unsafe actions",
      baseline: (
        <span className="text-danger">{formatNumber(baseline.ungatedActionsCount)}</span>
      ),
      pulsecover: (
        <span
          className={cn(
            (pulsecover.unsafeActionCount ?? 0) === 0 ? "text-success" : "text-danger",
          )}
        >
          {formatNumber(pulsecover.unsafeActionCount ?? 0)}
        </span>
      ),
      delta:
        (pulsecover.unsafeActionCount ?? 0) === 0 ? (
          <DeltaChip tone="success">safety invariant held</DeltaChip>
        ) : (
          <DeltaChip tone="danger">invariant violated</DeltaChip>
        ),
      hint: "Executed with no policy-gate ALLOW and no human approval — must be 0.",
    },
  ];

  return (
    <div>
      {/* Arm header — slate vs amber column ticks */}
      <div className="grid grid-cols-[1fr_auto_auto] items-center gap-x-6 border-b border-border pb-2">
        <span />
        <span className="flex items-center gap-1.5 justify-self-end font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
          <span
            aria-hidden
            className="size-[7px] rounded-full"
            style={{ backgroundColor: CHART_PALETTE.slate }}
          />
          Baseline
        </span>
        <span className="flex items-center gap-1.5 justify-self-end font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
          <span
            aria-hidden
            className="size-[7px] rounded-full"
            style={{ backgroundColor: CHART_PALETTE.accent }}
          />
          PulseRecover
        </span>
      </div>

      <dl>
        {rows.map((row) => (
          <div
            key={row.key}
            className="grid grid-cols-[1fr_auto_auto] items-baseline gap-x-6 border-b border-border py-2.5 last:border-0"
          >
            <dt className="text-xs text-text-3">
              {row.label}
              {row.hint ? (
                <span className="mt-0.5 block text-[11px] text-text-3/80">{row.hint}</span>
              ) : null}
            </dt>
            <dd className="justify-self-end font-mono text-sm tabular-nums text-text-2">
              {row.baseline}
            </dd>
            <dd
              className={cn(
                "flex items-center gap-2 justify-self-end font-mono text-sm tabular-nums",
                row.pulsecoverTone === "success"
                  ? "text-success"
                  : row.pulsecoverTone === "danger"
                    ? "text-danger"
                    : "text-text",
              )}
            >
              {row.pulsecover}
              {row.delta}
            </dd>
          </div>
        ))}
      </dl>

      {baseline.failedPaymentsCount !== undefined ? (
        <p className="mt-3 font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
          Same seeded scenario per arm · {formatNumber(baseline.failedPaymentsCount)} failed
          payments · {formatINR(baseline.failedAmountPaise ?? 0, { compact: true })} failed amount
          {pulsecover.approvalsRequired !== undefined
            ? ` · ${formatNumber(pulsecover.approvalsRequired)} human approvals in the PulseRecover arm`
            : ""}
        </p>
      ) : null}
    </div>
  );
}

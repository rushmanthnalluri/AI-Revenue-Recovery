import * as React from "react";

import type {
  EvalArm,
  EvalComparison,
  EvalHoldout,
} from "@/components/evaluation/evaluation-metrics";
import { formatNumber, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * "Operational outcomes" — the measured, action-level facts of a run, kept
 * as their own group so they can never be conflated with the holdout's
 * incremental-lift estimate: a high verified conversion on executed actions
 * is an operational fact, NOT evidence of fleet-level causal lift.
 *
 * Every value is stored in the run's metrics JSON; the only derivation is
 * the verified-conversion share (recovered_actions_count ÷
 * interventions_count) — the same ratio docs/evaluation.md §3 reports.
 */
interface EvaluationOperationalProps {
  baseline: EvalArm;
  pulsecover: EvalArm;
  comparison: EvalComparison | null;
  holdout: EvalHoldout | null;
}

function Fact({
  label,
  value,
  sub,
  tone = "default",
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: "default" | "success" | "danger";
}) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-bg px-3.5 py-3">
      <dt className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">{label}</dt>
      <dd
        className={cn(
          "mt-1.5 font-mono text-sm tabular-nums",
          tone === "success" ? "text-success" : tone === "danger" ? "text-danger" : "text-text",
        )}
      >
        {value}
      </dd>
      {sub ? <dd className="mt-0.5 text-[11px] leading-relaxed text-text-3">{sub}</dd> : null}
    </div>
  );
}

export function EvaluationOperational({
  baseline,
  pulsecover,
  comparison,
  holdout,
}: EvaluationOperationalProps) {
  const executed = pulsecover.interventionsCount;
  const recovered = pulsecover.recoveredActionsCount;
  const conversion =
    executed !== undefined && executed > 0 && recovered !== undefined
      ? recovered / executed
      : undefined;
  const unsafe = pulsecover.unsafeActionCount ?? 0;
  const organic = holdout?.holdout;

  return (
    <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <Fact
        label="Verified recovery (action-attributed)"
        value={
          <>
            {formatNumber(recovered)} / {formatNumber(executed)} actions ·{" "}
            {formatPercent(conversion)}
          </>
        }
        sub="Webhook/resolve-verified RECOVERED actions over executed interventions — the PulseRecover arm's recovery standard."
      />
      <Fact
        label="Baseline recovery standard"
        value="gross · unverified"
        sub="The naive arm counts gateway-twin captures with no verification stage — a gross recovery figure, not comparable to the verified one above."
      />
      {organic ? (
        <Fact
          label="Organic baseline (measured)"
          value={
            <>
              {formatPercent(organic.recoveryRate)} · {formatNumber(organic.recoveredPayments)} /{" "}
              {formatNumber(organic.failedPayments)}
            </>
          }
          sub="No-action holdout failures that self-resolved at the measured late-capture rate — the counterfactual the lift is measured against."
        />
      ) : null}
      <Fact
        label="Interventions"
        value={
          <>
            {formatNumber(pulsecover.interventionsCount)} vs{" "}
            {formatNumber(baseline.interventionsCount)} baseline
            {comparison?.interventionReduction !== undefined &&
            comparison.interventionReduction !== null
              ? ` · ${formatPercent(comparison.interventionReduction)} fewer`
              : ""}
          </>
        }
        sub="Customer-facing actions fired; the baseline retries every failed payment."
      />
      <Fact
        label="Unsafe actions"
        value={formatNumber(unsafe)}
        tone={unsafe === 0 ? "success" : "danger"}
        sub={
          unsafe === 0
            ? "Safety invariant held — every execution was gate-ALLOWED or human-approved."
            : "Invariant violated — executions without a gate ALLOW or human approval."
        }
      />
    </dl>
  );
}

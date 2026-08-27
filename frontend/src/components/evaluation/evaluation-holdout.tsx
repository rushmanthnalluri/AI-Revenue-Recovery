import * as React from "react";

import { CHART_PALETTE } from "@/components/chart-theme";
import type {
  EvalHoldout,
  EvalLift,
  EvalLiftStratum,
} from "@/components/evaluation/evaluation-metrics";
import { formatDeltaPP, formatMinutes, formatNumber, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * "Incremental lift (holdout-adjusted)" — the randomized-holdout readout.
 * Every value is a stored number from the run's `metrics.holdout` payload;
 * nothing is recomputed client-side. Older runs without the section never
 * reach this component (the parent hides it).
 */

type Tone = "success" | "danger" | "neutral";

function ciTone(lift: EvalLift): Tone {
  if (lift.ci95Low === undefined || lift.ci95High === undefined) return "neutral";
  if (lift.ci95Low > 0) return "success";
  if (lift.ci95High < 0) return "danger";
  return "neutral";
}

function LiftChip({ lift, label }: { lift: EvalLift; label: string }) {
  const tone = ciTone(lift);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-sm border px-[7px] py-[2px] font-mono text-[10px] uppercase tracking-[0.07em]",
        tone === "success" && "border-transparent bg-success-dim text-success",
        tone === "danger" && "border-transparent bg-danger-dim text-danger",
        tone === "neutral" && "border-border-strong text-text-3",
      )}
    >
      {label}: {lift.point !== undefined ? formatDeltaPP(lift.point) : "—"}
      {lift.ci95Low !== undefined && lift.ci95High !== undefined
        ? ` · 95% CI [${formatDeltaPP(lift.ci95Low)}, ${formatDeltaPP(lift.ci95High)}]`
        : ""}
    </span>
  );
}

function GroupColumn({
  label,
  color,
  recovered,
  failed,
  rate,
  medianTtr,
}: {
  label: string;
  color: string;
  recovered?: number;
  failed?: number;
  rate?: number;
  medianTtr?: number | null;
}) {
  return (
    <div className="rounded-md border border-border bg-bg px-3.5 py-3">
      <p className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
        <span aria-hidden className="size-[7px] rounded-full" style={{ backgroundColor: color }} />
        {label}
      </p>
      <p className="mt-1.5 font-mono text-lg tabular-nums text-text">
        {formatPercent(rate)}
      </p>
      <p className="mt-0.5 font-mono text-[11px] tabular-nums text-text-3">
        {formatNumber(recovered)} / {formatNumber(failed)} failed payments recovered
      </p>
      <p className="mt-0.5 font-mono text-[11px] tabular-nums text-text-3">
        median time-to-recovery {formatMinutes(medianTtr)}
      </p>
    </div>
  );
}

function StrataTable({
  title,
  rows,
  ariaLabel,
}: {
  title: string;
  rows: EvalLiftStratum[];
  ariaLabel: string;
}) {
  if (rows.length === 0) return null;
  return (
    <div>
      <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
        {title}
      </p>
      <div className="overflow-auto rounded-lg border border-border">
        <table className="w-full text-left" aria-label={ariaLabel}>
          <thead>
            <tr className="bg-surface">
              {["stratum", "treatment", "holdout", "lift · 95% CI"].map((h) => (
                <th
                  key={h}
                  className="border-b border-border px-3.5 py-2 font-mono text-[10px] uppercase tracking-[0.09em] text-text-3"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const tone = ciTone(row.lift);
              return (
                <tr key={row.stratum} className="border-b border-border last:border-0 hover:bg-surface">
                  <td className="px-3.5 py-2.5 font-mono text-xs text-text">{row.stratum}</td>
                  <td className="px-3.5 py-2.5 font-mono text-xs tabular-nums text-text-2">
                    {formatNumber(row.treatment.recoveredPayments)}/
                    {formatNumber(row.treatment.failedPayments)} (
                    {formatPercent(row.treatment.recoveryRate)})
                  </td>
                  <td className="px-3.5 py-2.5 font-mono text-xs tabular-nums text-text-2">
                    {formatNumber(row.holdout.recoveredPayments)}/
                    {formatNumber(row.holdout.failedPayments)} (
                    {formatPercent(row.holdout.recoveryRate)})
                  </td>
                  <td
                    className={cn(
                      "px-3.5 py-2.5 font-mono text-xs tabular-nums",
                      tone === "success" && "text-success",
                      tone === "danger" && "text-danger",
                      tone === "neutral" && "text-text-2",
                    )}
                  >
                    {row.lift.point !== undefined ? formatDeltaPP(row.lift.point) : "—"}
                    {row.lift.ci95Low !== undefined && row.lift.ci95High !== undefined ? (
                      <span className="text-text-3">
                        {" "}
                        [{formatDeltaPP(row.lift.ci95Low)}, {formatDeltaPP(row.lift.ci95High)}]
                      </span>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function EvaluationHoldout({ holdout }: { holdout: EvalHoldout }) {
  const { treatment, holdout: control, lift, liftAdjusted } = holdout;

  return (
    <div className="space-y-4">
      {/* Headline: the pre-registered ITT contrast + the mix-adjusted check */}
      <div className="flex flex-wrap items-center gap-2">
        <LiftChip lift={lift} label="Lift (ITT)" />
        {liftAdjusted?.point !== undefined ? (
          <LiftChip lift={liftAdjusted} label="Class-adjusted" />
        ) : null}
        {holdout.realizedFraction !== undefined ? (
          <span className="font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
            holdout {formatPercent(holdout.realizedFraction)} of customers · seed{" "}
            {holdout.seed ?? "—"} · deterministic
          </span>
        ) : null}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <GroupColumn
          label="Treatment (full loop)"
          color={CHART_PALETTE.accent}
          recovered={treatment.recoveredPayments}
          failed={treatment.failedPayments}
          rate={treatment.recoveryRate}
          medianTtr={treatment.medianTimeToRecoverMinutes}
        />
        <GroupColumn
          label="Holdout (no action)"
          color={CHART_PALETTE.slate}
          recovered={control.recoveredPayments}
          failed={control.failedPayments}
          rate={control.recoveryRate}
          medianTtr={control.medianTimeToRecoverMinutes}
        />
      </div>

      <p className="font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
        attribution window: failure → scenario end
        {holdout.attributionWindowHours !== undefined && holdout.attributionWindowHours !== null
          ? ` (max ${formatMinutes(holdout.attributionWindowHours * 60)})`
          : ""}
        {" · "}customers {formatNumber(holdout.customersTreatment)} treatment /{" "}
        {formatNumber(holdout.customersHoldout)} holdout
        {treatment.recoveredViaAction !== undefined
          ? ` · treatment recovered ${formatNumber(treatment.recoveredViaAction)} via actions + ${formatNumber(treatment.recoveredOrganic)} organic`
          : ""}
        {holdout.holdoutActionsCount !== undefined
          ? ` · holdout actions ${formatNumber(holdout.holdoutActionsCount)} (isolation)`
          : ""}
      </p>

      <StrataTable
        title="By failure class"
        rows={holdout.strataByFailureClass}
        ariaLabel="Per-failure-class recovery rates and lift with confidence intervals"
      />
      <StrataTable
        title="By payment method"
        rows={holdout.strataByMethod}
        ariaLabel="Per-method recovery rates and lift with confidence intervals"
      />

      {/* Methodology — honest framing, mirrors docs/evaluation.md */}
      <div className="rounded-md border border-border bg-bg px-3.5 py-3">
        <p className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
          Methodology
        </p>
        <ul className="mt-1.5 space-y-1.5">
          {(holdout.notes.length > 0
            ? holdout.notes
            : [
                "Deterministic customer-level holdout; denominators are ALL first-attempt failed payments per group; recovery counts verified captures only.",
              ]
          ).map((note) => (
            <li key={note} className="text-xs leading-relaxed text-text-3">
              {note}
            </li>
          ))}
          <li className="text-xs leading-relaxed text-text-3">
            Simulated fleet, disclosed harness roles: the operator (approves gated actions)
            and the customer (documented conversion priors, identical across groups). CI:
            Newcombe/Wilson 95% for the ITT contrast; pooled-weight post-stratification
            for the class-adjusted check. Tiny strata yield wide bands by design — never
            a bare point estimate.
          </li>
        </ul>
      </div>
    </div>
  );
}

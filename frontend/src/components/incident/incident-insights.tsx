"use client";

import * as React from "react";
import { Network, Store } from "lucide-react";

import type { IncidentDetail, IncidentInsights, InsightsOutlier } from "@/lib/types";
import { formatDateTime, formatNumber, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/empty-state";
import { Badge } from "@/components/ui/badge";

const OUTLIER_BASES = new Set(["failure_rate", "failure_share"]);
const CALLOUT_CLASSES = new Set(["platform_wide", "incident_specific"]);

function isFiniteNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

/** Defensively parse the additive `insights` field — older backends omit it. */
export function extractInsights(detail: IncidentDetail): IncidentInsights | null {
  const raw: unknown = detail.insights;
  if (typeof raw !== "object" || raw === null) return null;
  const rawRec = raw as Record<string, unknown>;
  const cf = rawRec.computed_from;
  if (typeof cf !== "object" || cf === null) return null;
  const cfRec = cf as Record<string, unknown>;
  if (typeof cfRec.window_start !== "string" || typeof cfRec.window_end !== "string") {
    return null;
  }
  const outliersRaw = rawRec.outliers;
  const outliers: InsightsOutlier[] = Array.isArray(outliersRaw)
    ? outliersRaw.flatMap((e): InsightsOutlier[] => {
        if (typeof e !== "object" || e === null) return [];
        const rec = e as Record<string, unknown>;
        if (typeof rec.dimension !== "string" || typeof rec.value !== "string") return [];
        if (!isFiniteNumber(rec.incident_rate) || !isFiniteNumber(rec.baseline_rate)) {
          return [];
        }
        return [
          {
            dimension: rec.dimension,
            value: rec.value,
            basis: OUTLIER_BASES.has(rec.basis as string)
              ? (rec.basis as InsightsOutlier["basis"])
              : "failure_rate",
            incident_rate: rec.incident_rate,
            baseline_rate: rec.baseline_rate,
            lift: isFiniteNumber(rec.lift) ? rec.lift : null,
            support: isFiniteNumber(rec.support) ? rec.support : 0,
            window_group_size: isFiniteNumber(rec.window_group_size)
              ? rec.window_group_size
              : 0,
            baseline_group_size: isFiniteNumber(rec.baseline_group_size)
              ? rec.baseline_group_size
              : 0,
            low_confidence: rec.low_confidence === true,
          },
        ];
      })
    : [];
  const calloutRaw = rawRec.platform_callout;
  const callout =
    typeof calloutRaw === "object" && calloutRaw !== null
      ? (() => {
          const rec = calloutRaw as Record<string, unknown>;
          if (typeof rec.summary !== "string") return null;
          return {
            dimension: typeof rec.dimension === "string" ? rec.dimension : "",
            value: typeof rec.value === "string" ? rec.value : "",
            classification: CALLOUT_CLASSES.has(rec.classification as string)
              ? (rec.classification as "platform_wide" | "incident_specific")
              : "incident_specific",
            platform_scope:
              typeof rec.platform_scope === "string"
                ? rec.platform_scope
                : "simulated_fleet",
            platform_window_rate: isFiniteNumber(rec.platform_window_rate)
              ? rec.platform_window_rate
              : 0,
            platform_baseline_rate: isFiniteNumber(rec.platform_baseline_rate)
              ? rec.platform_baseline_rate
              : 0,
            platform_lift: isFiniteNumber(rec.platform_lift) ? rec.platform_lift : null,
            platform_support: isFiniteNumber(rec.platform_support)
              ? rec.platform_support
              : 0,
            summary: rec.summary,
          };
        })()
      : null;
  return {
    outliers,
    platform_callout: callout,
    computed_from: {
      window_start: cfRec.window_start,
      window_end: cfRec.window_end,
      baseline_start:
        typeof cfRec.baseline_start === "string" ? cfRec.baseline_start : "",
      baseline_end: typeof cfRec.baseline_end === "string" ? cfRec.baseline_end : "",
      segment:
        typeof cfRec.segment === "object" && cfRec.segment !== null
          ? (cfRec.segment as Record<string, string>)
          : {},
      window_payments: isFiniteNumber(cfRec.window_payments) ? cfRec.window_payments : 0,
      window_failures: isFiniteNumber(cfRec.window_failures) ? cfRec.window_failures : 0,
      baseline_payments: isFiniteNumber(cfRec.baseline_payments)
        ? cfRec.baseline_payments
        : 0,
      baseline_failures: isFiniteNumber(cfRec.baseline_failures)
        ? cfRec.baseline_failures
        : 0,
    },
  };
}

/** Amber (platform-wide) / slate (incident-specific) benchmark banner. */
function CalloutBanner({ callout }: { callout: NonNullable<IncidentInsights["platform_callout"]> }) {
  const platformWide = callout.classification === "platform_wide";
  const Icon = platformWide ? Network : Store;
  return (
    <div
      className={cn(
        "flex items-start gap-2.5 rounded-lg border px-4 py-3.5 text-[13px] leading-relaxed text-text-2",
        platformWide
          ? "border-accent-border bg-accent-wash"
          : "border-[rgba(110,143,160,0.4)] bg-info-dim",
      )}
    >
      <Icon
        className={cn("mt-0.5 size-4 shrink-0", platformWide ? "text-accent" : "text-info")}
        strokeWidth={1.5}
        aria-hidden
      />
      <div className="min-w-0">
        <p
          className={cn(
            "font-mono text-[10px] uppercase tracking-[0.09em]",
            platformWide ? "text-accent" : "text-info",
          )}
        >
          {platformWide ? "platform-wide pattern" : "incident-specific pattern"} ·{" "}
          {callout.platform_scope === "simulated_fleet"
            ? "simulated fleet"
            : callout.platform_scope}
        </p>
        <p className="mt-1">{callout.summary}</p>
      </div>
    </div>
  );
}

function LiftCell({ outlier, maxLift }: { outlier: InsightsOutlier; maxLift: number }) {
  const isNew = outlier.lift === null || outlier.lift === undefined;
  const width = isNew
    ? 100
    : maxLift > 0
      ? Math.min(100, ((outlier.lift ?? 0) / maxLift) * 100)
      : 0;
  return (
    <div className="flex items-center gap-2">
      <span
        className={cn(
          "font-mono text-xs tabular-nums",
          isNew ? "text-accent" : "text-text",
        )}
      >
        {isNew ? "new" : `×${(outlier.lift ?? 0).toFixed(1)}`}
      </span>
      <div
        className="h-1 w-16 overflow-hidden rounded-sm bg-raised"
        role="meter"
        aria-valuenow={isNew ? 100 : Math.round(width)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`Lift of ${outlier.dimension}=${outlier.value}`}
      >
        <div
          className={cn(
            "h-full rounded-sm transition-[width] duration-500 ease-apple",
            outlier.low_confidence ? "bg-info" : "bg-accent",
          )}
          style={{ width: `${width}%` }}
        />
      </div>
    </div>
  );
}

/**
 * Failure outliers: facets overrepresented among the incident window's
 * failures vs the pre-incident baseline, plus the merchant-vs-network
 * callout. Rates are failure rates within the facet (method/bank/gateway)
 * or shares of all failures (error_code/error_reason) — see the basis tag.
 */
export function IncidentInsightsPanel({ insights }: { insights: IncidentInsights | null }) {
  if (!insights) {
    return (
      <EmptyState
        title="Insights not available"
        description="Decline-outlier diagnostics could not be computed for this incident (missing or malformed window data)."
      />
    );
  }

  const outliers = insights.outliers ?? [];
  const cf = insights.computed_from;

  if (outliers.length === 0) {
    return (
      <EmptyState
        title="No failure outliers"
        description={
          cf.window_failures === 0
            ? `Zero failed payments in the incident window (${formatNumber(cf.window_payments)} payments) — nothing to rank.`
            : `${formatNumber(cf.window_failures)} failures in the incident window, but no facet cleared the overrepresentation floors (support ≥ 3, lift ≥ 1.5×) vs the baseline window.`
        }
      />
    );
  }

  const finiteLifts = outliers
    .map((o) => o.lift)
    .filter((l): l is number => l !== null && l !== undefined);
  const maxLift = Math.max(1, ...finiteLifts);
  const anyLowConfidence = outliers.some((o) => o.low_confidence);

  return (
    <div className="space-y-4">
      {insights.platform_callout ? (
        <CalloutBanner callout={insights.platform_callout} />
      ) : null}

      <div className="overflow-auto rounded-lg border border-border">
        <table className="w-full text-left">
          <thead>
            <tr className="bg-surface">
              {["dimension", "value", "incident", "baseline", "lift", "support"].map((h) => (
                <th
                  key={h}
                  className="px-3.5 py-2.5 font-mono text-[10px] font-medium uppercase tracking-[0.09em] text-text-3"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {outliers.map((o) => (
              <tr
                key={`${o.dimension}:${o.value}`}
                className="border-b border-border last:border-b-0 hover:bg-surface"
              >
                <td className="px-3.5 py-2.5">
                  <span className="font-mono text-xs text-text-2">{o.dimension}</span>
                  <span className="ml-1.5 font-mono text-[9px] uppercase tracking-[0.07em] text-text-3">
                    {o.basis === "failure_share" ? "share" : "rate"}
                  </span>
                </td>
                <td className="max-w-56 truncate px-3.5 py-2.5 font-mono text-xs text-text">
                  {o.value}
                </td>
                <td className="px-3.5 py-2.5 font-mono text-xs tabular-nums text-danger">
                  {formatPercent(o.incident_rate)}
                </td>
                <td className="px-3.5 py-2.5 font-mono text-xs tabular-nums text-text-3">
                  {formatPercent(o.baseline_rate)}
                </td>
                <td className="px-3.5 py-2.5">
                  <LiftCell outlier={o} maxLift={maxLift} />
                </td>
                <td className="px-3.5 py-2.5 font-mono text-xs tabular-nums text-text-2">
                  {formatNumber(o.support)}
                  {o.low_confidence ? (
                    <Badge variant="warning" className="ml-1.5">
                      low confidence
                    </Badge>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="font-mono text-2xs tabular-nums leading-relaxed text-text-3">
        window {formatDateTime(cf.window_start)} → {formatDateTime(cf.window_end)} · baseline{" "}
        {formatDateTime(cf.baseline_start)} → {formatDateTime(cf.baseline_end)} ·{" "}
        {formatNumber(cf.window_failures)}/{formatNumber(cf.window_payments)} failed in window,{" "}
        {formatNumber(cf.baseline_failures)}/{formatNumber(cf.baseline_payments)} at baseline
        {anyLowConfidence
          ? " · slate bars: support < 10 failures (thin sample — read as directional, not proven)"
          : ""}
      </p>
    </div>
  );
}

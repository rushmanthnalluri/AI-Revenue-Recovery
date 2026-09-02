"use client";

import * as React from "react";
import Link from "next/link";

import type { DashboardSummary, Environment, OpportunitySummary } from "@/lib/types";
import { formatINR, formatNumber, formatPercent, timeAgo } from "@/lib/format";
import { EmptyState } from "@/components/empty-state";
import { ErrorPanel } from "@/components/error-panel";
import { StatusPill } from "@/components/status-pill";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

function humanize(value: string): string {
  return value.replace(/_/g, " ");
}

interface MiniStatProps {
  label: string;
  value: string;
  tone?: "default" | "warning" | "danger";
}

const MINI_TONE: Record<NonNullable<MiniStatProps["tone"]>, string> = {
  default: "text-text",
  warning: "text-accent",
  danger: "text-danger",
};

function MiniStat({ label, value, tone = "default" }: MiniStatProps) {
  return (
    <div className="bg-surface px-3.5 py-3">
      <p className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">{label}</p>
      <p className={cn("mt-1 font-mono text-base leading-tight tabular-nums", MINI_TONE[tone])}>
        {value}
      </p>
    </div>
  );
}

interface RecoveryPipelineProps {
  summary: DashboardSummary | undefined;
  opportunities: OpportunitySummary[] | undefined;
  opportunitiesTotal: number | undefined;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  environment: Environment;
}

/**
 * Recovery opportunities summary: the recoverable pipeline totals from
 * dashboard.summary plus the five newest opportunities (real API rows).
 * Detail work happens on /recovery — this panel is the at-a-glance read.
 */
export function RecoveryPipeline({
  summary,
  opportunities,
  opportunitiesTotal,
  loading,
  error,
  onRetry,
  environment,
}: RecoveryPipelineProps) {
  if (error) {
    return <ErrorPanel error={error} onRetry={onRetry} title="Recovery data unavailable" />;
  }

  if (loading) {
    return (
      <div aria-busy="true" aria-label="Loading recovery pipeline" className="space-y-2">
        <Skeleton className="h-16 w-full" />
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-9 w-full" />
        ))}
      </div>
    );
  }

  const items = opportunities ?? [];

  return (
    <div className="space-y-4">
      {/* Pipeline totals — hairline-divided band, same idiom as MetricStrip */}
      <dl className="grid grid-cols-3 gap-px overflow-hidden rounded-lg border border-border bg-border">
        <MiniStat
          label="Recoverable"
          value={formatINR(summary?.recoverable_revenue_paise ?? 0, { compact: true })}
          tone="warning"
        />
        <MiniStat
          label="In flight"
          value={formatNumber(summary?.active_recoveries ?? 0)}
        />
        <MiniStat
          label="Awaiting approval"
          value={formatNumber(summary?.pending_approvals ?? 0)}
          tone={(summary?.pending_approvals ?? 0) > 0 ? "danger" : "default"}
        />
      </dl>

      {items.length === 0 ? (
        <EmptyState
          title="No opportunities yet"
          description={
            environment === "real_test"
              ? "Nothing is waiting to be recovered — this is zero open recovery work, not missing data. Opportunities are built once the worker's detection flags an incident in your observed Razorpay Test Mode activity, and land here automatically."
              : "Opportunities are built once an incident is diagnosed. Run a scenario from the Research Lab to see the pipeline fill."
          }
        />
      ) : (
        <ul className="divide-y divide-border" aria-label="Newest recovery opportunities">
          {items.map((opp) => (
            <li key={opp.id} className="flex items-center gap-3 py-2.5">
              <div className="min-w-0 flex-1">
                <p className="truncate text-[13px] text-text">{humanize(opp.opportunity_type)}</p>
                <p className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
                  {timeAgo(opp.created_at)}
                  {opp.confidence !== null && opp.confidence !== undefined
                    ? ` · conf ${formatPercent(opp.confidence, 0)}`
                    : ""}
                </p>
              </div>
              <span className="font-mono text-xs tabular-nums text-text">
                {formatINR(opp.expected_recovery_paise, { compact: true })}
              </span>
              <StatusPill status={opp.status} />
            </li>
          ))}
        </ul>
      )}

      {(opportunitiesTotal ?? 0) > items.length ? (
        <p className="text-xs text-text-3">
          Showing {items.length} of {formatNumber(opportunitiesTotal)} —{" "}
          <Link href="/recovery" className="text-accent hover:text-accent-hover">
            open the recovery console
          </Link>
          .
        </p>
      ) : null}
    </div>
  );
}

import * as React from "react";

import type { DashboardSummary } from "@/lib/types";
import { formatINR } from "@/lib/format";
import { ChainStrip } from "@/components/command-center/chain-strip";
import { StatusPill } from "@/components/status-pill";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

interface RevenueHeroProps {
  summary: DashboardSummary | undefined;
  loading: boolean;
}

interface ContextMetricProps {
  label: string;
  value: string;
  tone?: "default" | "success" | "warning" | "muted";
}

const CONTEXT_TONE: Record<NonNullable<ContextMetricProps["tone"]>, string> = {
  default: "text-text",
  success: "text-success",
  warning: "text-accent",
  muted: "text-text-2",
};

function ContextMetric({ label, value, tone = "default" }: ContextMetricProps) {
  return (
    <div>
      <p className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">{label}</p>
      <p className={cn("mt-1 font-mono text-lg leading-tight tabular-nums", CONTEXT_TONE[tone])}>
        {value}
      </p>
    </div>
  );
}

/**
 * The dominant read on the Command Center: revenue at risk, full-size. The
 * 5-second grasp for a first-time user is the big tabular figure plus one
 * plain-language line of context; the hairline-divided side block carries the
 * recoverable / recovered / lost split (all real dashboard.summary fields).
 */
export function RevenueHero({ summary, loading }: RevenueHeroProps) {
  if (loading || !summary) {
    return (
      <section
        aria-label="Revenue at risk"
        aria-busy="true"
        className="card-sheen rounded-lg border border-border bg-surface p-5 md:p-6"
      >
        <Skeleton className="h-3 w-36" />
        <Skeleton className="mt-4 h-12 w-64" />
        <Skeleton className="mt-3 h-4 w-80 max-w-full" />
      </section>
    );
  }

  const atRisk = summary.revenue_at_risk_paise;
  const open = summary.open_incidents;
  const degraded = atRisk > 0 || open > 0;

  return (
    <section
      aria-label="Revenue at risk"
      className="card-sheen rounded-lg border border-border bg-surface p-5 md:p-6"
    >
      <div className="flex flex-col gap-6 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 font-mono text-[11px] uppercase tracking-[0.11em] text-text-3">
            <span aria-hidden className="inline-block h-px w-[18px] bg-accent" />
            Revenue at risk
            <StatusPill
              status={degraded ? "degraded" : "ok"}
              pulse={degraded}
              className="ml-1"
            />
            {summary.revenue_at_risk_low_confidence ? (
              <Badge variant="warning" title="At least one open incident has a low-confidence point estimate">
                estimate · low confidence
              </Badge>
            ) : null}
          </p>
          <p
            className={cn(
              "mt-2.5 font-mono text-[36px] font-semibold leading-none tabular-nums md:text-[46px]",
              atRisk > 0 ? "text-danger" : "text-success",
            )}
          >
            {formatINR(atRisk)}
          </p>
          <p className="mt-2.5 text-[13px] text-text-2">
            {degraded
              ? `Across ${open} open incident${open === 1 ? "" : "s"} · ${formatINR(
                  summary.recoverable_revenue_paise ?? 0,
                  { compact: true },
                )} of it estimated recoverable`
              : "No open incidents — no revenue at risk right now."}
          </p>
        </div>

        <div
          className="flex flex-wrap gap-x-8 gap-y-4 md:border-l md:border-border md:pl-6"
          aria-label="Revenue breakdown"
        >
          <ContextMetric
            label="Recoverable"
            value={formatINR(summary.recoverable_revenue_paise ?? 0, { compact: true })}
            tone="warning"
          />
          <ContextMetric
            label="Recovered"
            value={formatINR(summary.recovered_revenue_paise, { compact: true })}
            tone="success"
          />
          <ContextMetric
            label="Confirmed lost"
            value={formatINR(summary.lost_revenue_paise, { compact: true })}
            tone="muted"
          />
        </div>
      </div>

      <ChainStrip summary={summary} />
    </section>
  );
}

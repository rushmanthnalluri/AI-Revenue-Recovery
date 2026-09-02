import * as React from "react";
import { ArrowRight, ShieldCheck } from "lucide-react";

import type { DashboardSummary, IncidentStatus } from "@/lib/types";
import { formatINR, formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Incident statuses that are past the diagnosis step (backend lifecycle). */
const PAST_DIAGNOSIS: readonly IncidentStatus[] = [
  "DIAGNOSED",
  "RECOVERING",
  "RESOLVED",
  "CLOSED",
];

interface ChainStage {
  key: string;
  label: string;
  value: string;
  caption: string;
  /** True when the stage currently carries something (count/amount > 0). */
  active: boolean;
  verified?: boolean;
}

/**
 * Recovery chain strip — the live detected → diagnosed → policy → action →
 * verified read, pinned to the bottom of the revenue hero. Every stage is
 * derived from `dashboard.summary` fields only (`open_incidents`,
 * `recent_incidents[].status`, `pending_approvals`, `active_recoveries`,
 * `recovered_revenue_paise`); stages whose basis the API does not return are
 * omitted rather than fabricated. Rendered only when the environment actually
 * has incidents or recoveries — the fresh-environment empty state replaces
 * the whole hero.
 */
export function ChainStrip({ summary }: { summary: DashboardSummary }) {
  const recent = summary.recent_incidents ?? [];
  const diagnosed = recent.filter((i) => PAST_DIAGNOSIS.includes(i.status)).length;

  const hasAnything =
    summary.open_incidents > 0 ||
    recent.length > 0 ||
    summary.active_recoveries > 0 ||
    summary.pending_approvals > 0 ||
    summary.recovered_revenue_paise > 0;

  if (!hasAnything) return null;

  const stages: ChainStage[] = [
    {
      key: "detected",
      label: "Detected",
      value: formatNumber(summary.open_incidents),
      caption: summary.open_incidents === 1 ? "open incident" : "open incidents",
      active: summary.open_incidents > 0,
    },
    // Diagnosis counts are only derivable over the recent-incidents window the
    // summary returns — the caption states that basis, never a global count.
    ...(recent.length > 0
      ? [
          {
            key: "diagnosed",
            label: "Diagnosed",
            value: formatNumber(diagnosed),
            caption: `of ${formatNumber(recent.length)} recent`,
            active: diagnosed > 0,
          } satisfies ChainStage,
        ]
      : []),
    {
      key: "policy",
      label: "Policy gate",
      value: formatNumber(summary.pending_approvals),
      caption: "awaiting approval",
      active: summary.pending_approvals > 0,
    },
    {
      key: "actions",
      label: "Actions",
      value: formatNumber(summary.active_recoveries),
      caption: "in flight",
      active: summary.active_recoveries > 0,
    },
    {
      key: "verified",
      label: "Verified",
      value: formatINR(summary.recovered_revenue_paise, { compact: true }),
      caption: "recovered · webhook-verified",
      active: summary.recovered_revenue_paise > 0,
      verified: true,
    },
  ];

  return (
    <div className="mt-5 border-t border-border pt-4">
      <p className="font-mono text-[10px] uppercase tracking-[0.11em] text-text-3">
        Recovery chain · detected → verified
      </p>
      <ol className="mt-2.5 flex flex-wrap items-stretch gap-x-3 gap-y-3">
        {stages.map((stage, i) => (
          <li key={stage.key} className="flex items-center gap-3">
            {i > 0 ? (
              <ArrowRight
                aria-hidden
                className="size-3.5 shrink-0 text-text-3"
                strokeWidth={1.5}
              />
            ) : null}
            <div className="min-w-0">
              <p className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
                {stage.label}
              </p>
              <p
                className={cn(
                  "mt-0.5 flex items-center gap-1.5 font-mono text-base leading-tight tabular-nums",
                  stage.verified && stage.active
                    ? "text-success"
                    : stage.active
                      ? "text-text"
                      : "text-text-3",
                )}
              >
                {stage.verified ? (
                  <ShieldCheck aria-hidden className="size-3.5 shrink-0" strokeWidth={1.5} />
                ) : null}
                {stage.value}
              </p>
              <p className="mt-0.5 text-[11px] text-text-3">{stage.caption}</p>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

"use client";

import * as React from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Activity, Zap } from "lucide-react";

import { api } from "@/lib/api";
import { formatDeltaPP, formatINR, formatNumber, formatPercent, timeAgo } from "@/lib/format";
import { DeltaBadge } from "@/components/delta-badge";
import { DemoControl } from "@/components/demo-control";
import { EmptyState } from "@/components/empty-state";
import { ErrorPanel } from "@/components/error-panel";
import { MetricStrip } from "@/components/metric-strip";
import { PageHeader } from "@/components/page-header";
import { SectionCard } from "@/components/section-card";
import { Skeleton } from "@/components/ui/skeleton";
import { RecentIncidentsTable } from "@/components/command-center/recent-incidents-table";
import { RecoveryPipeline } from "@/components/command-center/recovery-pipeline";
import { RevenueHero } from "@/components/command-center/revenue-hero";
import { SuccessRateChart } from "@/components/command-center/success-rate-chart";
import { SystemHealthCard } from "@/components/command-center/system-health-card";

/**
 * Revenue Command Center — the 5-second read on system state:
 *   1. dominant revenue-at-risk hero,
 *   2. hairline-divided KPI band (MetricStrip),
 *   3. 24h success-rate trend with baseline + incident markers, beside health,
 *   4. Demo Control (how judges drive the demo),
 *   5. recent degradation table + recovery pipeline summary.
 * Summary polls every 15s; every number is a real API value.
 */
export function CommandCenterScreen() {
  const summary = useQuery({
    queryKey: ["dashboard", "summary"],
    queryFn: () => api.dashboard.summary(),
    refetchInterval: 15_000,
  });

  const timeseries = useQuery({
    queryKey: ["dashboard", "timeseries", "payment_success_rate", "hour", 24],
    queryFn: () =>
      api.dashboard.timeseries({
        metric: "payment_success_rate",
        granularity: "hour",
        window_hours: 24,
      }),
    refetchInterval: 60_000,
  });

  const opportunities = useQuery({
    queryKey: ["recovery", "opportunities", "command-center"],
    queryFn: () => api.recovery.opportunities({ page: 1, page_size: 5 }),
    refetchInterval: 30_000,
  });

  const s = summary.data;
  const baseline = s?.payments_baseline_success_rate ?? null;
  const successDeltaPp = s && baseline !== null ? s.payments_success_rate - baseline : null;
  const recentIncidents = s?.recent_incidents ?? [];

  /** Fresh database: nothing observed, nothing detected — guide to Demo Control. */
  const isFreshEnvironment =
    s !== undefined &&
    s.payments_observed === 0 &&
    s.open_incidents === 0 &&
    s.recovered_revenue_paise === 0 &&
    recentIncidents.length === 0;

  return (
    <div className="space-y-8">
      <PageHeader
        title="Command Center"
        description="Revenue at risk, payment reliability, and the recovery pipeline — live."
        actions={
          <>
            {s?.generated_at ? (
              <span className="font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
                updated {timeAgo(s.generated_at)} · polls 15s
              </span>
            ) : null}
            <a
              href="#demo-control"
              className="font-mono text-[10px] uppercase tracking-[0.09em] text-accent transition-colors duration-150 ease-apple hover:text-accent-hover"
            >
              demo control ↓
            </a>
          </>
        }
      />

      {summary.isError ? (
        <ErrorPanel
          error={summary.error}
          onRetry={() => summary.refetch()}
          title="Dashboard summary unavailable"
        />
      ) : (
        <>
          {isFreshEnvironment ? (
            <EmptyState
              icon={Zap}
              title="No telemetry yet"
              description="This environment has no payment data. Trigger a demo scenario to seed the simulator and watch detection, diagnosis, and recovery run end-to-end."
              action={
                <a
                  href="#demo-control"
                  className="inline-flex h-9 items-center justify-center gap-2 rounded-md bg-accent px-4 text-[13px] font-medium text-accent-ink transition-colors duration-150 ease-apple hover:bg-accent-hover"
                >
                  Trigger a demo scenario
                </a>
              }
            />
          ) : null}

          <RevenueHero summary={s} loading={summary.isPending} />

          <MetricStrip
            className="xl:grid-cols-6"
            items={[
              {
                key: "recoverable",
                label: "Recoverable revenue",
                tone: "warning",
                loading: summary.isPending,
                value: formatINR(s?.recoverable_revenue_paise ?? 0, { compact: true }),
                hint: "recoverable share of the at-risk loss",
              },
              {
                key: "recovered",
                label: "Recovered revenue",
                tone: "success",
                loading: summary.isPending,
                value: formatINR(s?.recovered_revenue_paise ?? 0, { compact: true }),
                hint: s
                  ? `${formatINR(s.lost_revenue_paise, { compact: true })} confirmed lost`
                  : undefined,
              },
              {
                key: "recovery-rate",
                label: "Recovery rate",
                loading: summary.isPending,
                value: formatPercent(s?.recovery_rate ?? 0),
                hint: "recovered / (recovered + lost + at risk)",
              },
              {
                key: "active-incidents",
                label: "Active incidents",
                tone: (s?.open_incidents ?? 0) > 0 ? "danger" : "default",
                loading: summary.isPending,
                value: formatNumber(s?.open_incidents ?? 0),
                hint: s ? `${s.pending_approvals} approvals pending` : undefined,
              },
              {
                key: "success-rate",
                label: "Payment success rate",
                loading: summary.isPending,
                value: formatPercent(s?.payments_success_rate ?? 0),
                badge: <DeltaBadge value={successDeltaPp} format={(d) => formatDeltaPP(d)} />,
                hint: s
                  ? baseline !== null
                    ? `baseline ${formatPercent(baseline)} · ${formatNumber(s.payments_observed)} payments (1h)`
                    : `${formatNumber(s.payments_observed)} payments observed (1h)`
                  : undefined,
              },
              {
                key: "in-flight",
                label: "Recoveries in flight",
                loading: summary.isPending,
                value: formatNumber(s?.active_recoveries ?? 0),
                hint: "actions executing or verifying",
              },
            ]}
          />
        </>
      )}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <SectionCard
          title="Payment success rate — 24h"
          description={
            s && baseline !== null
              ? `Hourly · current ${formatPercent(s.payments_success_rate)} vs baseline ${formatPercent(baseline)}`
              : "Hourly, anchored to the latest payment event"
          }
          className="xl:col-span-2"
        >
          {timeseries.isPending ? (
            <Skeleton className="h-[260px] w-full" aria-busy="true" aria-label="Loading trend" />
          ) : timeseries.isError ? (
            <ErrorPanel error={timeseries.error} onRetry={() => timeseries.refetch()} />
          ) : timeseries.data.points.length === 0 ? (
            <EmptyState
              icon={Activity}
              title="No telemetry in this window"
              description="No payment outcomes were recorded in the last 24 hours. Trigger a demo scenario to generate traffic."
            />
          ) : (
            <SuccessRateChart
              points={timeseries.data.points}
              baseline={baseline}
              incidents={recentIncidents}
            />
          )}
        </SectionCard>

        <SystemHealthCard />
      </div>

      <DemoControl />

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <SectionCard
          title="Recent degradation"
          description="Latest detected incidents — open one to investigate"
          actions={
            <Link
              href="/incidents"
              className="text-xs font-medium text-accent transition-colors duration-150 ease-apple hover:text-accent-hover"
            >
              View all →
            </Link>
          }
          className="xl:col-span-2"
          contentClassName="pt-0"
        >
          <div className="pt-2">
            <RecentIncidentsTable incidents={recentIncidents} loading={summary.isPending} />
          </div>
        </SectionCard>

        <SectionCard
          title="Recovery pipeline"
          description="Opportunities and in-flight recoveries"
          actions={
            <Link
              href="/recovery"
              className="text-xs font-medium text-accent transition-colors duration-150 ease-apple hover:text-accent-hover"
            >
              Open →
            </Link>
          }
        >
          <RecoveryPipeline
            summary={s}
            opportunities={opportunities.data?.items}
            opportunitiesTotal={opportunities.data?.total}
            loading={opportunities.isPending || summary.isPending}
            error={opportunities.isError ? opportunities.error : null}
            onRetry={() => opportunities.refetch()}
          />
        </SectionCard>
      </div>
    </div>
  );
}

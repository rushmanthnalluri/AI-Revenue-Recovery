"use client";

import * as React from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, FlaskConical, Loader2, PlugZap, RefreshCw } from "lucide-react";

import { api } from "@/lib/api";
import { formatDeltaPP, formatINR, formatNumber, formatPercent, timeAgo } from "@/lib/format";
import { DeltaBadge } from "@/components/delta-badge";
import { EmptyState } from "@/components/empty-state";
import { useEnvironment } from "@/components/environment-provider";
import { ErrorPanel } from "@/components/error-panel";
import { MetricStrip } from "@/components/metric-strip";
import { useMerchantConnection } from "@/components/merchant-connection";
import { PageHeader } from "@/components/page-header";
import { ProvenanceChip } from "@/components/provenance";
import { SectionCard } from "@/components/section-card";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { RecentIncidentsTable } from "@/components/command-center/recent-incidents-table";
import { RecoveryPipeline } from "@/components/command-center/recovery-pipeline";
import { RevenueHero } from "@/components/command-center/revenue-hero";
import { SuccessRateChart } from "@/components/command-center/success-rate-chart";
import { SystemHealthCard } from "@/components/command-center/system-health-card";

/**
 * Revenue Command Center — the 5-second read on system state, scoped to the
 * active environment (real Razorpay Test Mode merchant by default; synthetic
 * research data when the Research Lab environment is selected):
 *   1. dominant revenue-at-risk hero,
 *   2. hairline-divided KPI band (MetricStrip) with a provenance chip,
 *   3. 24h success-rate trend with baseline + incident markers, beside health,
 *   4. recent degradation table + recovery pipeline summary.
 * When the real merchant is not connected — or connected with no observed
 * activity yet — the data chrome is replaced by one honest empty state.
 * Summary polls every 15s; every number is a real API value.
 */
export function CommandCenterScreen() {
  const { environment } = useEnvironment();
  const queryClient = useQueryClient();
  const connection = useMerchantConnection();

  const summary = useQuery({
    queryKey: ["dashboard", "summary", environment],
    queryFn: () => api.dashboard.summary(environment),
    refetchInterval: 15_000,
  });

  const timeseries = useQuery({
    queryKey: ["dashboard", "timeseries", environment, "payment_success_rate", "hour", 24],
    queryFn: () =>
      api.dashboard.timeseries({
        metric: "payment_success_rate",
        granularity: "hour",
        window_hours: 24,
        environment,
      }),
    refetchInterval: 60_000,
  });

  const opportunities = useQuery({
    queryKey: ["recovery", "opportunities", "command-center", environment],
    queryFn: () => api.recovery.opportunities({ page: 1, page_size: 5, environment }),
    refetchInterval: 30_000,
  });

  const sync = useMutation({
    mutationFn: () => api.merchant.sync(),
    onSuccess: () => {
      // A sync can change every observed surface — refetch from truth.
      void queryClient.invalidateQueries();
    },
  });

  const s = summary.data;
  const isReal = environment === "real_test";
  const baseline = s?.payments_baseline_success_rate ?? null;
  const successDeltaPp = s && baseline !== null ? s.payments_success_rate - baseline : null;
  const recentIncidents = s?.recent_incidents ?? [];

  /** Nothing observed in this environment yet. */
  const isFreshEnvironment =
    s !== undefined &&
    s.payments_observed === 0 &&
    s.open_incidents === 0 &&
    s.recovered_revenue_paise === 0 &&
    recentIncidents.length === 0;

  /** Real merchant with no live connection (or connection state unknown). */
  const showNotConnected = isReal && connection.data !== undefined && !connection.data.connected;
  const showConnectedNoData =
    isReal && connection.data?.connected === true && isFreshEnvironment;
  const showUnknownNoData =
    isReal && connection.data === undefined && !connection.isPending && isFreshEnvironment;
  const showResearchEmpty = !isReal && isFreshEnvironment;
  const showEmptyState =
    showNotConnected || showConnectedNoData || showUnknownNoData || showResearchEmpty;

  return (
    <div className="space-y-8">
      <PageHeader
        title="Command Center"
        description={
          isReal
            ? "Revenue at risk, payment reliability, and the recovery pipeline — live from Razorpay Test Mode."
            : "Revenue at risk, payment reliability, and the recovery pipeline — on the synthetic research dataset."
        }
        actions={
          <>
            <ProvenanceChip
              environment={environment}
              window={isReal ? "1h window" : undefined}
              records={isReal ? s?.payments_observed : undefined}
            />
            {s?.generated_at ? (
              <span className="font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
                updated {timeAgo(s.generated_at)} · polls 15s
              </span>
            ) : null}
          </>
        }
      />

      {summary.isError ? (
        <ErrorPanel
          error={summary.error}
          onRetry={() => summary.refetch()}
          title="Dashboard summary unavailable"
        />
      ) : showEmptyState ? (
        <>
          {showNotConnected || showUnknownNoData ? (
            <EmptyState
              icon={PlugZap}
              title="Connect Razorpay Test Mode to begin"
              description="PulseRecover analyzes your real test-mode payment activity — detection, diagnosis, and recovery run on what it observes. Connect your test keys to start."
              action={
                <div className="flex flex-wrap items-center justify-center gap-2">
                  <Link href="/settings" className={buttonVariants({ size: "sm" })}>
                    Go to Settings
                  </Link>
                  <Link
                    href="/research"
                    className={buttonVariants({ variant: "secondary", size: "sm" })}
                  >
                    Explore Research Lab
                  </Link>
                </div>
              }
            />
          ) : null}

          {showConnectedNoData ? (
            <EmptyState
              icon={Activity}
              title="No payment activity yet"
              description="Process your first test payment — PulseRecover analyzes observed activity automatically. Detection, diagnosis, and recovery appear here as activity lands."
              action={
                <Button size="sm" disabled={sync.isPending} onClick={() => sync.mutate()}>
                  {sync.isPending ? (
                    <Loader2 aria-hidden className="animate-spin" />
                  ) : (
                    <RefreshCw aria-hidden />
                  )}
                  {sync.isPending ? "Syncing" : "Sync now"}
                </Button>
              }
            />
          ) : null}

          {showResearchEmpty ? (
            <EmptyState
              icon={FlaskConical}
              title="The research dataset is empty"
              description="Seed synthetic payment traffic from the Research Lab to evaluate detection, diagnosis, and recovery end-to-end. This data never mixes with the real merchant environment."
              action={
                <Link href="/research" className={buttonVariants({ size: "sm" })}>
                  Open Research Lab
                </Link>
              }
            />
          ) : null}

          {sync.isError ? (
            <ErrorPanel error={sync.error} onRetry={() => sync.mutate()} title="Sync failed" />
          ) : null}

          {/* Connectivity debugging stays visible behind every empty state */}
          <SystemHealthCard />
        </>
      ) : (
        <>
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
                hint: "share of at-risk loss",
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
                hint: "recovered / all affected",
              },
              {
                key: "active-incidents",
                label: "Active incidents",
                tone: (s?.open_incidents ?? 0) > 0 ? "danger" : "default",
                loading: summary.isPending,
                value: formatNumber(s?.open_incidents ?? 0),
                hint: s
                  ? `${s.pending_approvals} approval${s.pending_approvals === 1 ? "" : "s"} pending`
                  : undefined,
              },
              {
                key: "success-rate",
                label: "Payment success rate",
                loading: summary.isPending,
                value: formatPercent(s?.payments_success_rate ?? 0),
                badge: <DeltaBadge value={successDeltaPp} format={(d) => formatDeltaPP(d)} />,
                hint: s
                  ? baseline !== null
                    ? `baseline ${formatPercent(baseline)} · ${formatNumber(s.payments_observed)} in 1h`
                    : `${formatNumber(s.payments_observed)} observed in 1h`
                  : undefined,
              },
              {
                key: "in-flight",
                label: "Recoveries in flight",
                loading: summary.isPending,
                value: formatNumber(s?.active_recoveries ?? 0),
                hint: "executing or verifying",
              },
            ]}
          />

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
                <Skeleton
                  className="h-[260px] w-full"
                  aria-busy="true"
                  aria-label="Loading trend"
                />
              ) : timeseries.isError ? (
                <ErrorPanel error={timeseries.error} onRetry={() => timeseries.refetch()} />
              ) : timeseries.data.points.length === 0 ? (
                <EmptyState
                  icon={Activity}
                  title="No telemetry in this window"
                  description={
                    isReal
                      ? "No payment outcomes were recorded in the last 24 hours."
                      : "No payment outcomes were recorded in the last 24 hours. Run a scenario from the Research Lab to generate synthetic traffic."
                  }
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
                <RecentIncidentsTable
                  incidents={recentIncidents}
                  loading={summary.isPending}
                  environment={environment}
                />
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
                environment={environment}
              />
            </SectionCard>
          </div>
        </>
      )}
    </div>
  );
}

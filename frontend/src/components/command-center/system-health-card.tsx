"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { SectionCard } from "@/components/section-card";
import { StatusPill } from "@/components/status-pill";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * System health panel. Shares the ["system","health"] cache entry owned by
 * AppShell (which polls it every 20s), so this card subscribes without
 * starting a second poller.
 */
export function SystemHealthCard() {
  const health = useQuery({
    queryKey: ["system", "health"],
    queryFn: () => api.system.health(),
  });

  const checks = Object.entries(health.data?.checks ?? {});

  return (
    <SectionCard
      title="System health"
      description="Backend components, polled every 20s"
      actions={
        health.data ? (
          <StatusPill status={health.data.status} pulse={health.data.status !== "ok"} />
        ) : undefined
      }
    >
      {health.isPending ? (
        <div aria-busy="true" aria-label="Loading system health" className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-8 w-full" />
          ))}
        </div>
      ) : health.isError ? (
        <ErrorPanel error={health.error} onRetry={() => health.refetch()} title="Health check failed" />
      ) : health.data ? (
        <div>
          <dl className="flex flex-wrap gap-x-6 gap-y-1 border-b border-border pb-3 font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
            <div className="flex items-center gap-1.5">
              <dt>version</dt>
              <dd className="normal-case text-text-2">{health.data.version}</dd>
            </div>
            <div className="flex items-center gap-1.5">
              <dt>env</dt>
              <dd className="normal-case text-text-2">{health.data.app_env}</dd>
            </div>
            <div className="flex items-center gap-1.5">
              <dt>gateway</dt>
              <dd>
                <Badge
                  variant={health.data.simulation_mode ? "info" : "success"}
                  title={
                    health.data.simulation_mode
                      ? "Backend is configured with the synthetic gateway twin — real Razorpay calls require keys and SIMULATION_MODE=false"
                      : "Backend is configured for live Razorpay API calls"
                  }
                >
                  {health.data.simulation_mode ? "synthetic gateway" : "razorpay api"}
                </Badge>
              </dd>
            </div>
          </dl>
          {checks.length === 0 ? (
            <p className="py-3 text-xs text-text-3">No component checks reported.</p>
          ) : (
            <ul className="divide-y divide-border">
              {checks.map(([name, check]) => (
                <li key={name} className="flex items-center justify-between gap-3 py-2">
                  <span className="text-xs text-text-2">{name.replace(/_/g, " ")}</span>
                  <span className="flex min-w-0 items-center gap-2">
                    {check.detail ? (
                      <span
                        className="max-w-[140px] truncate font-mono text-[10px] text-text-3"
                        title={check.detail}
                      >
                        {check.detail}
                      </span>
                    ) : null}
                    <StatusPill status={check.status} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </SectionCard>
  );
}

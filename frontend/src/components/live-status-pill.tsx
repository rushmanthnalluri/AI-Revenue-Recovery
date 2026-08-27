"use client";

import * as React from "react";
import type { UseQueryResult } from "@tanstack/react-query";

import type { SystemHealth } from "@/lib/types";
import { cn } from "@/lib/utils";

export type HealthQuery = UseQueryResult<SystemHealth, Error>;

type LiveState = "connecting" | "live" | "degraded" | "unreachable";

const STATE: Record<LiveState, { dot: string; label: string; pulse: boolean }> = {
  connecting: { dot: "bg-text-3", label: "API · Connecting", pulse: false },
  live: { dot: "bg-success", label: "API · Live", pulse: false },
  degraded: { dot: "bg-accent", label: "API · Degraded", pulse: true },
  unreachable: { dot: "bg-danger", label: "API · Offline", pulse: true },
};

function resolveState(health: HealthQuery): LiveState {
  if (health.isError) return "unreachable";
  if (health.isPending) return "connecting";
  return health.data && health.data.status !== "ok" ? "degraded" : "live";
}

/**
 * Mono live-status pill — spec: 7px status dot (success green when healthy),
 * mono uppercase micro-label, hairline border. The 1.4s pulse is reserved for
 * degraded/alert states. Fed by the shared /api/v1/system/health query owned
 * by AppShell, so the pill never polls on its own.
 */
export function LiveStatusPill({
  health,
  className,
}: {
  health: HealthQuery;
  className?: string;
}) {
  const state = STATE[resolveState(health)];
  return (
    <span
      role="status"
      aria-live="polite"
      title="Backend /api/v1/system/health, polled every 20s"
      className={cn(
        "inline-flex items-center gap-2 rounded-sm border border-border-strong bg-surface px-[9px] py-[5px] font-mono text-[9.5px] uppercase tracking-[0.07em] text-text-2",
        className,
      )}
    >
      <span
        aria-hidden
        className={cn(
          "size-[7px] rounded-full",
          state.dot,
          state.pulse && "animate-status-pulse",
        )}
      />
      {state.label}
    </span>
  );
}

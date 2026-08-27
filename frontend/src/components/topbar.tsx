"use client";

import * as React from "react";

import { LiveStatusPill, type HealthQuery } from "@/components/live-status-pill";
import { Badge } from "@/components/ui/badge";

/**
 * Sticky top header — the one sanctioned glassmorphism use: bg-bg/80 +
 * 18px backdrop blur over a hairline bottom border. Carries the env /
 * simulation badges; the live-status pill appears here only below md (on
 * desktop it lives in the sidebar footer).
 */
export function Topbar({ health }: { health: HealthQuery }) {
  const env = health.data?.app_env;
  const simulation = health.data?.simulation_mode;

  return (
    <header className="sticky top-0 z-20 flex h-14 items-center justify-between gap-4 border-b border-border bg-bg/80 px-4 backdrop-blur-[18px] md:pl-8">
      <div className="flex items-center gap-3">
        {/* Mobile wordmark (sidebar hidden below md) */}
        <span className="text-[13.5px] font-semibold tracking-tight text-text md:hidden">
          PulseRecover
        </span>
        <span className="hidden font-mono text-[10px] uppercase tracking-[0.11em] text-text-3 md:inline">
          Payment Reliability &amp; Revenue Recovery Engine
        </span>
      </div>

      <div className="flex items-center gap-2">
        {env ? (
          <Badge variant="outline" title="Backend APP_ENV">
            {env}
          </Badge>
        ) : null}
        {simulation ? (
          <Badge variant="accent" title="Gateway is in simulation mode — no live Razorpay calls">
            simulation
          </Badge>
        ) : null}
        <LiveStatusPill health={health} className="md:hidden" />
      </div>
    </header>
  );
}

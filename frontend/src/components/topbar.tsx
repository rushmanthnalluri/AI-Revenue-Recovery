"use client";

import * as React from "react";

import { ConnectionBadge } from "@/components/connection-badge";
import { useEnvironment } from "@/components/environment-provider";
import { LiveStatusPill, type HealthQuery } from "@/components/live-status-pill";
import { MobileNav } from "@/components/mobile-nav";

/**
 * Sticky top header — the one sanctioned glassmorphism use: bg-bg/80 +
 * 18px backdrop blur over a hairline bottom border. Carries the environment
 * truth badge (Razorpay Test Mode connection state / Synthetic Research);
 * the live-status pill appears here only below md (on desktop it lives in
 * the sidebar footer). While the Research Lab environment is active, a slim
 * persistent strip under the header keeps the synthetic-data boundary on
 * screen on every route.
 */
export function Topbar({ health }: { health: HealthQuery }) {
  const { environment } = useEnvironment();

  return (
    <header className="sticky top-0 z-20 border-b border-border bg-bg/80 backdrop-blur-[18px]">
      <div className="flex h-14 items-center justify-between gap-4 px-4 md:pl-8">
        <div className="flex items-center gap-3">
          <MobileNav />
          {/* Mobile wordmark (sidebar hidden below md) */}
          <span className="text-[13.5px] font-semibold tracking-tight text-text md:hidden">
            PulseRecover
          </span>
          <span className="hidden font-mono text-[10px] uppercase tracking-[0.11em] text-text-3 md:inline">
            Payment Reliability &amp; Revenue Recovery Engine
          </span>
        </div>

        <div className="flex items-center gap-2">
          <ConnectionBadge />
          <LiveStatusPill health={health} className="md:hidden" />
        </div>
      </div>

      {environment === "research" ? (
        <div
          role="note"
          className="border-t border-border bg-info-dim px-4 py-1 text-center font-mono text-[9.5px] uppercase tracking-[0.11em] text-info md:pl-8"
        >
          Synthetic research — simulator data, not merchant activity
        </div>
      ) : null}
    </header>
  );
}

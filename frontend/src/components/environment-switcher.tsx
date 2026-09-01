"use client";

import * as React from "react";

import { useEnvironment } from "@/components/environment-provider";
import { ENVIRONMENT_SWITCH_LABEL } from "@/lib/environment";
import { ENVIRONMENTS } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Prominent two-segment environment switcher — REAL MERCHANT vs RESEARCH LAB.
 * Lives at the top of the sidebar (and inside the mobile drawer) so the data
 * boundary is always one glance away. Active segment uses the amber accent
 * wash for the real merchant surface and the sanctioned slate for research.
 */
export function EnvironmentSwitcher() {
  const { environment, setEnvironment } = useEnvironment();

  return (
    <div>
      <p className="px-2 pb-1.5 font-mono text-[10px] uppercase tracking-[0.11em] text-text-3">
        Environment
      </p>
      <div
        role="group"
        aria-label="Data environment"
        className="grid grid-cols-2 gap-1 rounded-lg border border-border bg-bg p-1"
      >
        {ENVIRONMENTS.map((env) => {
          const active = environment === env;
          return (
            <button
              key={env}
              type="button"
              aria-pressed={active}
              onClick={() => setEnvironment(env)}
              className={cn(
                "rounded-md px-2 py-2 font-mono text-[10px] font-medium uppercase tracking-[0.09em] transition-colors duration-150 ease-apple",
                active
                  ? env === "real_test"
                    ? "bg-accent-dim text-accent shadow-[inset_0_0_0_1px_rgba(217,166,63,0.4)]"
                    : "bg-info-dim text-info shadow-[inset_0_0_0_1px_rgba(110,143,160,0.45)]"
                  : "text-text-3 hover:bg-raised hover:text-text-2",
              )}
            >
              {ENVIRONMENT_SWITCH_LABEL[env]}
            </button>
          );
        })}
      </div>
    </div>
  );
}

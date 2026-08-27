import * as React from "react";

import { formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

interface ConfidenceBarProps {
  /** 0..1 confidence score. */
  value: number;
  showLabel?: boolean;
  className?: string;
}

/**
 * Model-confidence meter. Thresholds mirror the policy gate: ≥0.85 green
 * (auto-execution band), ≥0.6 amber, below that red.
 */
export function ConfidenceBar({ value, showLabel = true, className }: ConfidenceBarProps) {
  const clamped = Math.min(1, Math.max(0, value));
  const pct = Math.round(clamped * 100);
  const tone = clamped >= 0.85 ? "bg-success" : clamped >= 0.6 ? "bg-accent" : "bg-danger";

  return (
    <div className={cn("flex items-center gap-2", className)}>
      <div
        role="meter"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Confidence"
        className="h-1.5 w-20 overflow-hidden rounded-sm bg-raised"
      >
        <div
          className={cn("h-full rounded-sm transition-[width] duration-500 ease-apple", tone)}
          style={{ width: `${pct}%` }}
        />
      </div>
      {showLabel ? (
        <span className="font-mono text-xs tabular-nums text-text-3">{formatPercent(clamped, 0)}</span>
      ) : null}
    </div>
  );
}

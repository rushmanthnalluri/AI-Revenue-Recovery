import * as React from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export interface MetricStripItem {
  key: string;
  label: string;
  value: React.ReactNode;
  tone?: "default" | "success" | "warning" | "danger";
  /** Slot for a DeltaBadge or StatusPill rendered next to the label. */
  badge?: React.ReactNode;
  hint?: React.ReactNode;
  loading?: boolean;
}

interface MetricStripProps {
  items: MetricStripItem[];
  className?: string;
}

const toneValue: Record<NonNullable<MetricStripItem["tone"]>, string> = {
  default: "text-text",
  success: "text-success",
  warning: "text-accent",
  danger: "text-danger",
};

/**
 * KPI metric strip — spec: not cards. One hairline-framed band; cells are
 * divided by 1px hairlines (the gap-px over border-colour technique), values
 * are 23px mono tabular numerals, labels are quiet micro-labels.
 */
export function MetricStrip({ items, className }: MetricStripProps) {
  return (
    <dl
      className={cn(
        "card-sheen grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border md:grid-cols-3",
        className,
      )}
    >
      {items.map((item) => (
        <div key={item.key} className="bg-surface px-4 py-3.5">
          <dt className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-text-3">
            <span className="truncate" title={item.label}>
              {item.label}
            </span>
            {item.badge}
          </dt>
          {item.loading ? (
            <Skeleton className="mt-1.5 h-7 w-24" />
          ) : (
            <dd
              className={cn(
                "mt-1 font-mono text-[23px] leading-tight tabular-nums",
                toneValue[item.tone ?? "default"],
              )}
            >
              {item.value}
            </dd>
          )}
          {item.loading ? (
            <Skeleton className="mt-1.5 h-3 w-32" />
          ) : (
            item.hint ? <dd className="mt-0.5 truncate text-xs text-text-3">{item.hint}</dd> : null
          )}
        </div>
      ))}
    </dl>
  );
}

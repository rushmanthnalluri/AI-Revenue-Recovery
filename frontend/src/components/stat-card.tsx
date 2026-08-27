import * as React from "react";
import type { LucideIcon } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

interface StatCardProps {
  label: string;
  value: React.ReactNode;
  icon?: LucideIcon;
  hint?: React.ReactNode;
  /** Slot for a DeltaBadge or StatusPill rendered next to the label. */
  badge?: React.ReactNode;
  tone?: "default" | "success" | "warning" | "danger";
  loading?: boolean;
  className?: string;
}

const toneValue: Record<NonNullable<StatCardProps["tone"]>, string> = {
  default: "text-text",
  success: "text-success",
  warning: "text-accent",
  danger: "text-danger",
};

const toneIcon: Record<NonNullable<StatCardProps["tone"]>, string> = {
  default: "text-text-3",
  success: "text-success/70",
  warning: "text-accent/70",
  danger: "text-danger/70",
};

/**
 * Single-metric panel. For the primary KPI rows prefer MetricStrip (the
 * hairline-divided band mandated by the design spec); StatCard remains for
 * one-off metrics that need a standalone panel.
 */
export function StatCard({
  label,
  value,
  icon: Icon,
  hint,
  badge,
  tone = "default",
  loading = false,
  className,
}: StatCardProps) {
  return (
    <Card className={className}>
      <CardContent className="p-4">
        <div className="flex items-center justify-between gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
            {label}
          </span>
          {Icon ? (
            <Icon className={cn("size-[18px]", toneIcon[tone])} strokeWidth={1.5} aria-hidden />
          ) : null}
        </div>
        {loading ? (
          <Skeleton className="mt-2 h-7 w-24" />
        ) : (
          <div className={cn("mt-1.5 font-mono text-[23px] leading-tight tabular-nums", toneValue[tone])}>
            {value}
          </div>
        )}
        {loading ? (
          <Skeleton className="mt-2 h-3 w-32" />
        ) : (
          (badge || hint) && (
            <div className="mt-1.5 flex items-center gap-2 text-xs text-text-3">
              {badge}
              {hint}
            </div>
          )
        )}
      </CardContent>
    </Card>
  );
}

import * as React from "react";
import { Minus, TrendingDown, TrendingUp } from "lucide-react";

import { cn } from "@/lib/utils";

interface DeltaBadgeProps {
  /** Signed delta; pass null/undefined to render nothing. */
  value: number | null | undefined;
  /** Formats the delta for display (e.g. "+2.4 pp"). Defaults to fixed 1-decimal signed. */
  format?: (delta: number) => string;
  /** When true, a negative delta is the good outcome (e.g. MTTR). */
  invert?: boolean;
  className?: string;
}

/**
 * Colored trend chip: green when the delta is a good outcome, red when bad,
 * neutral when flat. Good direction is configurable via `invert`.
 */
export function DeltaBadge({ value, format, invert = false, className }: DeltaBadgeProps) {
  if (value === null || value === undefined || Number.isNaN(value)) return null;

  const flat = Math.abs(value) < 1e-9;
  const good = flat ? null : invert ? value < 0 : value > 0;
  const Icon = flat ? Minus : value > 0 ? TrendingUp : TrendingDown;
  const text = format ? format(value) : `${value > 0 ? "+" : ""}${value.toFixed(1)}`;

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-sm px-[7px] py-[3px] font-mono text-[9.5px] tabular-nums",
        flat && "bg-raised text-text-3",
        good === true && "bg-success-dim text-success",
        good === false && "bg-danger-dim text-danger",
        className,
      )}
    >
      <Icon className="size-3" strokeWidth={1.5} aria-hidden />
      {text}
    </span>
  );
}

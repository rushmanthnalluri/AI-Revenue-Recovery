import * as React from "react";

import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

export interface TimelineItem {
  id: string;
  title: React.ReactNode;
  timestamp?: string | null;
  description?: React.ReactNode;
  tone?: "success" | "warning" | "danger" | "info" | "neutral";
}

const DOT: Record<NonNullable<TimelineItem["tone"]>, string> = {
  success: "bg-success",
  warning: "bg-accent",
  danger: "bg-danger",
  info: "bg-info",
  neutral: "bg-text-3",
};

/** Vertical event timeline for incident/audit narratives. */
export function Timeline({ items, className }: { items: TimelineItem[]; className?: string }) {
  return (
    <ol className={cn("relative space-y-4 border-l border-border pl-5", className)}>
      {items.map((item) => (
        <li key={item.id} className="relative">
          <span
            aria-hidden
            className={cn(
              "absolute -left-[26px] top-1.5 size-2.5 rounded-full ring-4 ring-surface",
              DOT[item.tone ?? "neutral"],
            )}
          />
          <div className="flex flex-wrap items-baseline justify-between gap-x-4">
            <p className="text-sm font-medium text-text">{item.title}</p>
            {item.timestamp ? (
              <time
                dateTime={item.timestamp}
                className="font-mono text-2xs tabular-nums text-text-3"
              >
                {formatDateTime(item.timestamp)}
              </time>
            ) : null}
          </div>
          {item.description ? (
            <div className="mt-0.5 text-xs text-text-3">{item.description}</div>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

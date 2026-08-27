import * as React from "react";
import { Inbox, type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}

/** Consistent empty state — never fabricated placeholder numbers. */
export function EmptyState({
  icon: Icon = Inbox,
  title,
  description,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border-strong px-6 py-10 text-center",
        className,
      )}
    >
      <Icon className="size-6 text-text-3" strokeWidth={1.5} aria-hidden />
      <p className="text-sm font-medium text-text">{title}</p>
      {description ? <p className="max-w-md text-xs text-text-3">{description}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

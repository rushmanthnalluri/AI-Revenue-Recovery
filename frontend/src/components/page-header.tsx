import * as React from "react";

import { cn } from "@/lib/utils";

interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: React.ReactNode;
  className?: string;
}

/** Page-level heading block used at the top of every route. The kicker row
    follows the spec: mono uppercase micro-label preceded by an 18px × 1px
    amber tick. */
export function PageHeader({ title, description, actions, className }: PageHeaderProps) {
  return (
    <div className={cn("flex flex-wrap items-start justify-between gap-3", className)}>
      <div>
        <p className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.11em] text-text-3">
          <span aria-hidden className="inline-block h-px w-[18px] bg-accent" />
          PulseRecover
        </p>
        <h1 className="mt-1.5 text-lg font-semibold tracking-tight text-text">{title}</h1>
        {description ? <p className="mt-0.5 text-sm text-text-2">{description}</p> : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </div>
  );
}

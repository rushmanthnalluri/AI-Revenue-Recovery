import * as React from "react";
import Link from "next/link";

import { cn } from "@/lib/utils";

interface AuditEntityChipProps {
  entityType: string;
  entityId: string;
  className?: string;
}

/**
 * Mono chip for the entity an audit row mutates — `inc_…` / `act_…` /
 * `pol_…` ids rendered in IBM Plex Mono with the entity type as a quiet
 * prefix. Incident rows deep-link to the incident detail screen; other
 * entity types stay inert (their owners render them inside drawers).
 */
export function AuditEntityChip({ entityType, entityId, className }: AuditEntityChipProps) {
  const chip = (
    <>
      <span className="text-text-3">{entityType}</span>
      <span aria-hidden className="text-text-3">
        ·
      </span>
      <span className="text-text-2">{entityId}</span>
    </>
  );

  const classes = cn(
    "inline-flex max-w-full items-center gap-1.5 rounded-sm border border-border-strong px-[7px] py-[2px] font-mono text-[10px] tracking-[0.02em] transition-colors duration-150 ease-apple",
    className,
  );

  if (entityType === "incident") {
    return (
      <Link
        href={`/incidents/${encodeURIComponent(entityId)}`}
        title={`Open incident ${entityId}`}
        className={cn(classes, "hover:border-text-3 hover:text-text")}
      >
        {chip}
      </Link>
    );
  }

  return (
    <span title={`${entityType} ${entityId}`} className={classes}>
      {chip}
    </span>
  );
}

"use client";

import * as React from "react";
import { Bot, CircleDot, Cpu, Link2, User, type LucideIcon } from "lucide-react";

import { formatDateTime } from "@/lib/format";
import type { AuditLogEntry } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  AUDIT_TONE_TILE,
  auditActionMeta,
} from "@/components/audit/audit-action-meta";
import { AuditEntityChip } from "@/components/audit/audit-entity-chip";
import { AuditEntryDetails } from "@/components/audit/audit-entry-details";
import { EnvironmentBadge } from "@/components/provenance";

export function auditAnchorId(entryId: string): string {
  return `audit-${entryId}`;
}

const ACTOR_ICONS: Record<string, LucideIcon> = {
  human: User,
  agent: Bot,
  system: Cpu,
};

function ActorChip({ actor }: { actor: string }) {
  const prefix = actor.includes(":") ? actor.slice(0, actor.indexOf(":")) : "";
  const Icon = ACTOR_ICONS[prefix] ?? CircleDot;
  return (
    <span
      title={`actor: ${actor}`}
      className="inline-flex items-center gap-1.5 rounded-sm border border-border-strong px-[7px] py-[2px] font-mono text-[10px] tracking-[0.02em] text-text-2"
    >
      <Icon className="size-3 text-text-3" strokeWidth={1.5} aria-hidden />
      {actor}
    </span>
  );
}

interface AuditTimelineProps {
  entries: AuditLogEntry[];
}

/**
 * Chronological audit stream — one rail, one icon tile per action type,
 * mono timestamps, actor + entity chips, expandable raw JSON details.
 *
 * Deep links: every row has a stable `#audit-<entry id>` anchor; visiting
 * with such a hash scrolls the row into view and highlights it, and the
 * link button on each row copies that anchor into the address bar.
 */
export function AuditTimeline({ entries }: AuditTimelineProps) {
  const [highlighted, setHighlighted] = React.useState<string | null>(null);

  // Resolve the initial (or changed) hash to a row: highlight + scroll. The
  // scroll fires only when the hash actually changes — poll refreshes must
  // never yank the reader back to the linked row.
  const highlightedRef = React.useRef<string | null>(null);
  React.useEffect(() => {
    const applyHash = () => {
      const hash = window.location.hash.replace(/^#/, "");
      if (!hash) {
        highlightedRef.current = null;
        setHighlighted(null);
        return;
      }
      const target = document.getElementById(hash);
      if (target) {
        const isNew = highlightedRef.current !== hash;
        highlightedRef.current = hash;
        setHighlighted(hash);
        if (isNew) target.scrollIntoView({ block: "center" });
      }
    };
    applyHash();
    window.addEventListener("hashchange", applyHash);
    return () => window.removeEventListener("hashchange", applyHash);
  }, [entries]);

  return (
    <ol className="relative space-y-4 before:absolute before:inset-y-1 before:left-[13px] before:w-px before:bg-border">
      {entries.map((entry) => {
        const meta = auditActionMeta(entry.action, entry.entity_type);
        const anchorId = auditAnchorId(entry.id);
        const isHighlighted = highlighted === anchorId;
        const hasDetails = entry.details != null && Object.keys(entry.details).length > 0;

        return (
          <li key={entry.id} className="relative pl-10">
            <span
              aria-hidden
              className={cn(
                "absolute left-0 top-0 z-10 flex size-[27px] items-center justify-center rounded-md border bg-surface",
                AUDIT_TONE_TILE[meta.tone],
              )}
            >
              <meta.icon className="size-[15px]" strokeWidth={1.5} />
            </span>

            <div
              id={anchorId}
              className={cn(
                "scroll-mt-24 rounded-lg border px-3.5 py-2.5 transition-colors duration-150 ease-apple",
                isHighlighted
                  ? "border-accent-border bg-accent-wash"
                  : "border-transparent hover:border-border",
              )}
            >
              <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                <p className="text-sm font-medium text-text">
                  {meta.label}
                  <span className="ml-2 font-mono text-[10px] font-normal tracking-[0.02em] text-text-3">
                    {entry.action}
                  </span>
                </p>
                <span className="flex items-center gap-2">
                  <time
                    dateTime={entry.created_at}
                    title={entry.created_at}
                    className="font-mono text-2xs tabular-nums text-text-3"
                  >
                    {formatDateTime(entry.created_at)}
                  </time>
                  <a
                    href={`#${anchorId}`}
                    aria-label={`Link to audit entry ${entry.id}`}
                    title="Copy anchor link"
                    className="rounded-sm text-text-3 transition-colors duration-150 ease-apple hover:text-accent"
                  >
                    <Link2 className="size-3.5" strokeWidth={1.5} aria-hidden />
                  </a>
                </span>
              </div>

              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                <EnvironmentBadge environment={entry.environment} />
                <ActorChip actor={entry.actor} />
                <AuditEntityChip entityType={entry.entity_type} entityId={entry.entity_id} />
                {entry.request_id ? (
                  <span
                    title={`request id: ${entry.request_id}`}
                    className="font-mono text-[10px] tabular-nums text-text-3"
                  >
                    req {entry.request_id}
                  </span>
                ) : null}
              </div>

              {hasDetails ? (
                <div className="mt-1.5">
                  <AuditEntryDetails entryId={entry.id} details={entry.details!} />
                </div>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

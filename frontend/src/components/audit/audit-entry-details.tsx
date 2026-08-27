"use client";

import * as React from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

interface AuditEntryDetailsProps {
  entryId: string;
  details: Record<string, unknown>;
}

/**
 * Expandable JSON details block for one audit row — a keyboard-focusable
 * toggle revealing the raw `details` payload as pretty-printed mono JSON.
 * Raw payload, never summarized: the audit trail is the ground record.
 */
export function AuditEntryDetails({ entryId, details }: AuditEntryDetailsProps) {
  const [open, setOpen] = React.useState(false);
  const panelId = `audit-details-${entryId}`;
  const json = React.useMemo(() => JSON.stringify(details, null, 2), [details]);

  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 rounded-sm font-mono text-[10px] uppercase tracking-[0.07em] text-text-3 transition-colors duration-150 ease-apple hover:text-text"
      >
        <ChevronRight
          className={cn("size-3 transition-transform duration-150 ease-apple", open && "rotate-90")}
          strokeWidth={1.5}
          aria-hidden
        />
        {open ? "Hide details" : "Details"}
      </button>
      {open ? (
        <pre
          id={panelId}
          className="mt-2 max-h-64 overflow-auto rounded-md border border-border bg-bg p-3 font-mono text-[11px] leading-relaxed text-text-2"
        >
          {json}
        </pre>
      ) : null}
    </div>
  );
}

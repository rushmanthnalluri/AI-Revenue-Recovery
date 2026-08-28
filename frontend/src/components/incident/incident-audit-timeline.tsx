import * as React from "react";

import type { IncidentTimelineEvent } from "@/lib/types";
import { Timeline, type TimelineItem } from "@/components/timeline";
import { EmptyState } from "@/components/empty-state";

const KIND_TONE: Record<string, TimelineItem["tone"]> = {
  detected: "danger",
  status_change: "info",
  evidence_added: "neutral",
  diagnosis: "warning",
  action: "info",
  note: "neutral",
};

/**
 * Audit trail for the incident — detection, evidence collection, diagnoses,
 * status changes and recovery actions in chronological order, newest last
 * (the API already sorts ascending).
 *
 * Bulk builds emit one event per opportunity (100+ near-identical rows that
 * bury detection/diagnosis/policy events), so consecutive events with the
 * same summary, kind and actor collapse into one row with a ×N count.
 * Event-level detail remains on the Audit Trail screen.
 */
export function IncidentAuditTimeline({ events }: { events: IncidentTimelineEvent[] | undefined }) {
  if (!events || events.length === 0) {
    return (
      <EmptyState
        title="No audit events"
        description="Detection, diagnosis and recovery events will appear here as they happen."
      />
    );
  }

  const grouped: { event: IncidentTimelineEvent; count: number }[] = [];
  for (const event of events) {
    const last = grouped[grouped.length - 1];
    if (
      last &&
      last.event.summary === event.summary &&
      last.event.kind === event.kind &&
      last.event.actor === event.actor
    ) {
      last.count += 1;
    } else {
      grouped.push({ event, count: 1 });
    }
  }

  const items: TimelineItem[] = grouped.map(({ event, count }, i) => ({
    id: `${event.ts}-${event.kind}-${i}`,
    title:
      count > 1 ? (
        <span className="inline-flex flex-wrap items-center gap-2">
          {event.summary}
          <span className="rounded-sm border border-border-strong px-[7px] py-[3px] font-mono text-[9.5px] uppercase tracking-[0.07em] text-text-2">
            ×{count}
          </span>
        </span>
      ) : (
        event.summary
      ),
    timestamp: event.ts,
    tone: KIND_TONE[event.kind] ?? "neutral",
    description: (
      <span className="font-mono text-2xs uppercase tracking-[0.07em]">
        {event.kind.replace(/_/g, " ")} · {event.actor}
      </span>
    ),
  }));

  return <Timeline items={items} />;
}

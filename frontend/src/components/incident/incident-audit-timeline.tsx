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

  const items: TimelineItem[] = events.map((e, i) => ({
    id: `${e.ts}-${e.kind}-${i}`,
    title: e.summary,
    timestamp: e.ts,
    tone: KIND_TONE[e.kind] ?? "neutral",
    description: (
      <span className="font-mono text-2xs uppercase tracking-[0.07em]">
        {e.kind.replace(/_/g, " ")} · {e.actor}
      </span>
    ),
  }));

  return <Timeline items={items} />;
}

"use client";

import * as React from "react";

import type { EvidenceItem } from "@/lib/types";
import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/empty-state";
import {
  formatDeviation,
  formatMetricValue,
  metricDirection,
} from "@/components/incident/incident-metric";

export interface SegmentEntry {
  value: string;
  events: number;
  baseline: number | null;
  observed: number | null;
  deviation_pct: number | null;
  flagged: boolean;
}

/** Extract the segment_breakdown evidence: dimension → ranked segment rows. */
export function extractSegmentBreakdown(
  evidence: EvidenceItem[] | undefined,
): Record<string, SegmentEntry[]> | null {
  const item = evidence?.find(
    (e) => e.evidence_type === "segment_breakdown" && e.payload?.dimensions,
  );
  if (!item) return null;
  const raw = item.payload.dimensions;
  if (typeof raw !== "object" || raw === null) return null;
  const out: Record<string, SegmentEntry[]> = {};
  for (const [dimension, entries] of Object.entries(raw as Record<string, unknown>)) {
    if (!Array.isArray(entries)) continue;
    out[dimension] = entries
      .map((e): SegmentEntry | null => {
        if (typeof e !== "object" || e === null) return null;
        const rec = e as Record<string, unknown>;
        if (typeof rec.value !== "string") return null;
        const maybeNum = (v: unknown) =>
          typeof v === "number" && Number.isFinite(v) ? v : null;
        return {
          value: rec.value,
          events: typeof rec.events === "number" ? rec.events : 0,
          baseline: maybeNum(rec.baseline),
          observed: maybeNum(rec.observed),
          deviation_pct: maybeNum(rec.deviation_pct),
          flagged: rec.flagged === true,
        };
      })
      .filter((e): e is SegmentEntry => e !== null);
  }
  return Object.keys(out).length > 0 ? out : null;
}

interface IncidentSegmentBreakdownProps {
  dimensions: Record<string, SegmentEntry[]> | null;
  metric: string;
}

/** One segment row: value, deviation bar scaled within its dimension, counts. */
function SegmentRow({
  entry,
  metric,
  maxAbsDeviation,
}: {
  entry: SegmentEntry;
  metric: string;
  maxAbsDeviation: number;
}) {
  const dev = entry.deviation_pct;
  const degraded =
    dev !== null && (metricDirection(metric) === "down" ? dev < 0 : dev > 0);
  const width = dev !== null && maxAbsDeviation > 0 ? (Math.abs(dev) / maxAbsDeviation) * 100 : 0;

  return (
    <li className="py-2">
      <div className="flex items-baseline justify-between gap-3">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate font-mono text-xs text-text">{entry.value}</span>
          {entry.flagged ? (
            <span className="rounded-sm bg-danger-dim px-[5px] py-[1px] font-mono text-[9px] uppercase tracking-[0.07em] text-danger">
              flagged
            </span>
          ) : null}
        </span>
        <span
          className={cn(
            "font-mono text-xs tabular-nums",
            dev === null ? "text-text-3" : degraded ? "text-danger" : "text-success",
          )}
        >
          {formatDeviation(dev)}
        </span>
      </div>
      <div
        className="mt-1.5 h-1 w-full overflow-hidden rounded-sm bg-raised"
        role="meter"
        aria-valuenow={dev !== null ? Math.round(Math.abs(dev)) : 0}
        aria-valuemin={0}
        aria-valuemax={Math.ceil(maxAbsDeviation)}
        aria-label={`Relative deviation of ${entry.value}`}
      >
        <div
          className={cn(
            "h-full rounded-sm transition-[width] duration-500 ease-apple",
            entry.flagged ? "bg-danger" : "bg-info",
          )}
          style={{ width: `${Math.min(100, width)}%` }}
        />
      </div>
      <p className="mt-1 font-mono text-2xs tabular-nums text-text-3">
        {formatNumber(entry.events)} events · baseline {formatMetricValue(metric, entry.baseline)} →
        observed {formatMetricValue(metric, entry.observed)}
      </p>
    </li>
  );
}

/**
 * Segment contributions to the incident (method / bank / gateway), straight
 * from the detector's localization evidence. Bars scale to the largest
 * absolute deviation within each dimension.
 */
export function IncidentSegmentBreakdown({ dimensions, metric }: IncidentSegmentBreakdownProps) {
  const names = dimensions ? Object.keys(dimensions) : [];

  if (
    !dimensions ||
    names.length === 0 ||
    names.every((d) => (dimensions[d] ?? []).length === 0)
  ) {
    return (
      <EmptyState
        title="No segment breakdown"
        description="The detector did not attach localization evidence for this incident."
      />
    );
  }

  return (
    <div className="grid gap-6 md:grid-cols-3">
      {names.map((dimension) => {
        const entries = dimensions[dimension] ?? [];
        const maxAbs = Math.max(0, ...entries.map((e) => Math.abs(e.deviation_pct ?? 0)));
        return (
          <section key={dimension} aria-label={`Segments by ${dimension}`}>
            <h4 className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
              by {dimension}
            </h4>
            {entries.length === 0 ? (
              <p className="mt-2 text-xs text-text-3">No contributing segments.</p>
            ) : (
              <ul className="divide-y divide-border">{entries.map((e) => (
                <SegmentRow
                  key={e.value}
                  entry={e}
                  metric={metric}
                  maxAbsDeviation={maxAbs}
                />
              ))}</ul>
            )}
          </section>
        );
      })}
    </div>
  );
}

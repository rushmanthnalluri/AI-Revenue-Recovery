"use client";

import * as React from "react";
import { useReducedMotion } from "framer-motion";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  CHART_PALETTE,
  MONO_FONT,
  cartesianGridProps,
  getAnimationProps,
  tooltipProps,
  xAxisProps,
  yAxisProps,
} from "@/components/chart-theme";
import type { EvidenceItem } from "@/lib/types";
import { formatTime } from "@/lib/format";
import { formatMetricValue, isRatioMetric } from "@/components/incident/incident-metric";

interface Bucket {
  ts: string;
  value: number | null;
  count: number;
}

/** Extract the metric_series snapshot from an incident's evidence list. */
export function extractMetricSeries(evidence: EvidenceItem[] | undefined): Bucket[] | null {
  const item = evidence?.find(
    (e) => e.evidence_type === "metric_series" && Array.isArray(e.payload?.buckets),
  );
  if (!item) return null;
  const buckets = (item.payload.buckets as unknown[])
    .map((b): Bucket | null => {
      if (typeof b !== "object" || b === null) return null;
      const rec = b as Record<string, unknown>;
      if (typeof rec.ts !== "string") return null;
      return {
        ts: rec.ts,
        value: typeof rec.value === "number" && Number.isFinite(rec.value) ? rec.value : null,
        count: typeof rec.count === "number" ? rec.count : 0,
      };
    })
    .filter((b): b is Bucket => b !== null);
  return buckets.length > 0 ? buckets : null;
}

/**
 * Anomaly start: the detector writes it into the incident description
 * ("flagged <metric> between <start> and <end> within window [...]"). Parse
 * it back out; fall back to the detection timestamp when the text differs.
 */
export function anomalyStartFrom(
  description: string | null | undefined,
  detectedAt: string,
): string {
  if (description) {
    const match = /between\s+(\S+)\s+and\s+(\S+)\s+within window/.exec(description);
    const candidate = match?.[1];
    if (candidate) {
      const parsed = new Date(candidate);
      if (!Number.isNaN(parsed.getTime())) return candidate;
    }
  }
  return detectedAt;
}

interface IncidentMetricChartProps {
  buckets: Bucket[];
  metric: string;
  baselineValue?: number | null;
  anomalyStart: string;
  height?: number;
}

/**
 * Metric timeline for an incident: the detector's own bucketed-series
 * evidence snapshot in the mandated recharts skin, with the baseline region
 * shaded (slate ReferenceArea), a dashed baseline level line, and a danger
 * dashed marker where the anomaly begins. Empty buckets (no events) stay as
 * honest gaps, never interpolated.
 */
export function IncidentMetricChart({
  buckets,
  metric,
  baselineValue,
  anomalyStart,
  height = 280,
}: IncidentMetricChartProps) {
  const reduced = Boolean(useReducedMotion());
  const fmt = React.useCallback((v: number) => formatMetricValue(metric, v), [metric]);

  const data = React.useMemo(
    () => buckets.map((b) => ({ ts: b.ts, value: b.value, count: b.count })),
    [buckets],
  );

  /* The marker needs an exact x value — snap the anomaly start to the first
     bucket at or after it. */
  const anomalyBucketTs = React.useMemo(() => {
    const start = new Date(anomalyStart).getTime();
    if (Number.isNaN(start)) return null;
    const hit = buckets.find((b) => new Date(b.ts).getTime() >= start);
    return hit ? hit.ts : null;
  }, [buckets, anomalyStart]);

  const firstTs = data[0]?.ts;
  const isRatio = isRatioMetric(metric);
  const yDomain = React.useMemo((): [number, number] | undefined => {
    if (isRatio) return [0, 1];
    const vals = data.map((d) => d.value).filter((v): v is number => v !== null);
    if (baselineValue !== null && baselineValue !== undefined) vals.push(baselineValue);
    if (vals.length === 0) return undefined;
    const max = Math.max(...vals);
    return [0, max > 0 ? max * 1.1 : 1];
  }, [data, baselineValue, isRatio]);

  const refLabel = (value: string, fill: string) => ({
    value,
    fill,
    fontSize: 10,
    fontFamily: MONO_FONT,
    position: "insideTopRight" as const,
  });

  return (
    <div
      style={{ height }}
      className="w-full"
      role="img"
      aria-label={`${metric} timeline: bucketed series snapshot with baseline region and anomaly start marker`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 16, right: 8, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="incidentMetricFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={CHART_PALETTE.accent} stopOpacity={0.32} />
              <stop offset="100%" stopColor={CHART_PALETTE.accent} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid {...cartesianGridProps} />
          <XAxis
            dataKey="ts"
            minTickGap={40}
            tickFormatter={(ts: string) => formatTime(ts)}
            {...xAxisProps}
          />
          <YAxis domain={yDomain} tickFormatter={(v: number) => fmt(v)} tickCount={6} {...yAxisProps} />
          <Tooltip
            {...tooltipProps}
            labelFormatter={(ts) => formatTime(String(ts))}
            formatter={(value, name) => {
              if (name === "count") return [String(value), "events"];
              return [typeof value === "number" ? fmt(value) : "no data", metric];
            }}
          />
          {firstTs && anomalyBucketTs && anomalyBucketTs !== firstTs ? (
            <ReferenceArea
              x1={firstTs}
              x2={anomalyBucketTs}
              fill={CHART_PALETTE.slate}
              fillOpacity={0.06}
              label={{ ...refLabel("baseline", CHART_PALETTE.tick), position: "insideTopLeft" }}
            />
          ) : null}
          {baselineValue !== null && baselineValue !== undefined ? (
            <ReferenceLine
              y={baselineValue}
              stroke={CHART_PALETTE.slate}
              strokeDasharray="4 4"
              strokeWidth={1}
              label={refLabel(`baseline ${fmt(baselineValue)}`, CHART_PALETTE.slate)}
            />
          ) : null}
          {anomalyBucketTs ? (
            <ReferenceLine
              x={anomalyBucketTs}
              stroke={CHART_PALETTE.danger}
              strokeDasharray="4 4"
              strokeWidth={1}
              label={refLabel("anomaly start", CHART_PALETTE.danger)}
            />
          ) : null}
          <Area
            type="monotone"
            dataKey="value"
            stroke={CHART_PALETTE.accent}
            strokeWidth={1.5}
            fill="url(#incidentMetricFill)"
            connectNulls={false}
            {...getAnimationProps(reduced)}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

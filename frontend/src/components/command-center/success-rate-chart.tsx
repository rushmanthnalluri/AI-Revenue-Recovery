"use client";

import * as React from "react";
import { useReducedMotion } from "framer-motion";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceDot,
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
import type { IncidentSummary, TimeSeriesPoint } from "@/lib/types";
import { formatDateTime, formatPercent, formatTime } from "@/lib/format";

interface SuccessRateChartProps {
  points: TimeSeriesPoint[];
  /** Baseline-window success rate (0..1); the dashed reference line. */
  baseline?: number | null;
  /** Open/recent incidents — rendered as danger markers at the nearest hour. */
  incidents?: IncidentSummary[];
  height?: number;
}

interface ChartRow {
  t: number;
  value: number;
}

interface IncidentMarker {
  id: string;
  t: number;
  value: number;
}

function toIso(t: number): string {
  return new Date(t).toISOString();
}

/** Nearest chart point within 45 minutes, else the incident is out of window. */
function nearestPoint(rows: ChartRow[], iso: string): ChartRow | null {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t) || rows.length === 0) return null;
  let best: ChartRow | null = null;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const row of rows) {
    const dist = Math.abs(row.t - t);
    if (dist < bestDist) {
      best = row;
      bestDist = dist;
    }
  }
  return bestDist <= 45 * 60 * 1000 ? best : null;
}

/**
 * 24h payment success-rate trend in the mandated recharts skin, plus the two
 * Command-Center-specific overlays: a dashed slate baseline reference line
 * (dashboard.summary baseline window) and danger dots marking incident
 * detections snapped to the nearest hourly bucket. The x axis is a numeric
 * time scale so markers land on the exact bucket even when an HH:MM label
 * repeats across the window boundary.
 */
export function SuccessRateChart({
  points,
  baseline = null,
  incidents = [],
  height = 260,
}: SuccessRateChartProps) {
  const reduced = Boolean(useReducedMotion());

  const data = React.useMemo<ChartRow[]>(
    () => points.map((p) => ({ t: new Date(p.ts).getTime(), value: p.value })),
    [points],
  );

  const markers = React.useMemo<IncidentMarker[]>(() => {
    const seen = new Set<number>();
    const out: IncidentMarker[] = [];
    for (const inc of incidents) {
      const point = nearestPoint(data, inc.detected_at);
      if (!point || seen.has(point.t)) continue;
      seen.add(point.t);
      out.push({ id: inc.id, t: point.t, value: point.value });
    }
    return out;
  }, [data, incidents]);

  const latest = points.length > 0 ? points[points.length - 1]!.value : null;
  const ariaLabel =
    latest === null
      ? "Hourly payment success rate, last 24 hours. No data yet."
      : `Hourly payment success rate, last 24 hours. Latest ${formatPercent(latest)}${
          baseline !== null ? `, baseline ${formatPercent(baseline)}` : ""
        }${markers.length > 0 ? `, ${markers.length} incident marker${markers.length === 1 ? "" : "s"}` : ""}.`;

  return (
    <div>
      <div role="img" aria-label={ariaLabel} style={{ height }} className="w-full">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id="ccSuccessFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={CHART_PALETTE.accent} stopOpacity={0.32} />
                <stop offset="100%" stopColor={CHART_PALETTE.accent} stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid {...cartesianGridProps} />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(t: number) => formatTime(toIso(t))}
              minTickGap={40}
              {...xAxisProps}
            />
            <YAxis
              domain={[0, 1]}
              tickFormatter={(v: number) => formatPercent(v, 0)}
              tickCount={6}
              {...yAxisProps}
            />
            <Tooltip
              {...tooltipProps}
              labelFormatter={(t) => formatDateTime(toIso(Number(t)))}
              formatter={(value) => [formatPercent(Number(value)), "success rate"]}
            />
            {baseline !== null ? (
              <ReferenceLine
                y={baseline}
                stroke={CHART_PALETTE.slate}
                strokeDasharray="4 4"
                strokeWidth={1}
                label={{
                  value: "baseline",
                  position: "insideTopRight",
                  fill: CHART_PALETTE.tick,
                  fontSize: 10,
                  fontFamily: MONO_FONT,
                }}
              />
            ) : null}
            <Area
              type="monotone"
              dataKey="value"
              stroke={CHART_PALETTE.accent}
              strokeWidth={1.5}
              fill="url(#ccSuccessFill)"
              {...getAnimationProps(reduced)}
            />
            {markers.map((m) => (
              <ReferenceDot
                key={m.id}
                x={m.t}
                y={m.value}
                r={4}
                fill={CHART_PALETTE.danger}
                stroke={CHART_PALETTE.tooltipBg}
                strokeWidth={1.5}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Legend — mono micro-labels, flat swatches, no emoji */}
      <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden className="inline-block h-0.5 w-4 bg-accent" />
          success rate
        </span>
        {baseline !== null ? (
          <span className="inline-flex items-center gap-1.5">
            <span
              aria-hidden
              className="inline-block h-0 w-4 border-t border-dashed border-info"
            />
            baseline {formatPercent(baseline)}
          </span>
        ) : null}
        {markers.length > 0 ? (
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden className="inline-block size-[7px] rounded-full bg-danger" />
            incident detected
          </span>
        ) : null}
      </div>
    </div>
  );
}

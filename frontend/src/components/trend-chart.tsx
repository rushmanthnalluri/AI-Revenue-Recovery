"use client";

import * as React from "react";
import { useReducedMotion } from "framer-motion";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  CHART_PALETTE,
  cartesianGridProps,
  getAnimationProps,
  tooltipProps,
  xAxisProps,
  yAxisProps,
} from "@/components/chart-theme";
import type { TimeSeriesPoint } from "@/lib/types";
import { formatTime } from "@/lib/format";

interface TrendChartProps {
  points: TimeSeriesPoint[];
  /** "percent" treats values as 0..1 ratios; "number" renders raw. */
  format?: "percent" | "number";
  height?: number;
  color?: string;
  label?: string;
}

function valueFormatter(format: "percent" | "number") {
  return (v: number) =>
    format === "percent" ? `${(v * 100).toFixed(1)}%` : v.toLocaleString("en-IN");
}

/**
 * Success-rate / metric trend chart in the mandated recharts skin
 * (docs/ui-design-system.md): y-grid-only hairlines, 10px IBM Plex Mono
 * ticks, no axis lines, flat amber series, #202521 floating tooltip, 450ms
 * ease-out animation (off under prefers-reduced-motion).
 */
export function TrendChart({
  points,
  format = "percent",
  height = 260,
  color = CHART_PALETTE.accent,
  label = "value",
}: TrendChartProps) {
  const reduced = Boolean(useReducedMotion());
  const data = React.useMemo(
    () => points.map((p) => ({ ts: p.ts, time: formatTime(p.ts), value: p.value })),
    [points],
  );
  const fmt = valueFormatter(format);
  const yDomain: [number, number] =
    format === "percent" ? [0, 1] : [0, Math.max(1, ...points.map((p) => p.value))];

  return (
    <div style={{ height }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="trendFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.32} />
              <stop offset="100%" stopColor={color} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid {...cartesianGridProps} />
          <XAxis dataKey="time" minTickGap={40} {...xAxisProps} />
          <YAxis
            domain={yDomain}
            tickFormatter={(v: number) => fmt(v)}
            {...yAxisProps}
            tickCount={6}
          />
          <Tooltip {...tooltipProps} formatter={(value) => [fmt(Number(value)), label]} />
          <Area
            type="monotone"
            dataKey="value"
            stroke={color}
            strokeWidth={1.5}
            fill="url(#trendFill)"
            {...getAnimationProps(reduced)}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

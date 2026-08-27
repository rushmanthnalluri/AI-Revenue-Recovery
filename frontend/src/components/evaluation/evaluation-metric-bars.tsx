"use client";

import * as React from "react";
import { useReducedMotion } from "framer-motion";
import {
  Bar,
  BarChart,
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
import { formatMinutes, formatNumber, formatPercent } from "@/lib/format";

export interface MetricBarDatum {
  key: string;
  label: string;
  /** Naive-retry arm value; null/undefined when the arm has no such stage. */
  baseline?: number | null;
  /** PulseRecover arm value. */
  pulsecover?: number | null;
}

interface EvaluationMetricBarsProps {
  data: MetricBarDatum[];
  /** percent = 0..1 ratios; number = counts; minutes = durations. */
  format?: "percent" | "number" | "minutes";
  height?: number;
  /** Accessible chart summary (rendered via role="img"). */
  ariaLabel: string;
  className?: string;
}

function formatter(format: "percent" | "number" | "minutes") {
  return (v: number) =>
    format === "percent"
      ? formatPercent(v)
      : format === "minutes"
        ? formatMinutes(v)
        : formatNumber(v);
}

/**
 * Grouped baseline-vs-PulseRecover bar chart in the mandated recharts skin
 * (docs/ui-design-system.md): y-grid-only hairlines, 10px IBM Plex Mono
 * ticks, no axis lines, amber = PulseRecover, slate = baseline, ~0.55-alpha
 * fills, 450ms ease-out animation (off under prefers-reduced-motion).
 * A series is dropped entirely when the arm never produced that metric.
 */
export function EvaluationMetricBars({
  data,
  format = "percent",
  height = 220,
  ariaLabel,
  className,
}: EvaluationMetricBarsProps) {
  const reduced = Boolean(useReducedMotion());
  const fmt = formatter(format);

  const hasBaseline = data.some((d) => d.baseline !== null && d.baseline !== undefined);
  const hasPulsecover = data.some((d) => d.pulsecover !== null && d.pulsecover !== undefined);

  return (
    <div className={className}>
      {/* Hand-rolled mono legend — recharts' default legend breaks the skin. */}
      <div className="mb-2 flex items-center gap-4">
        {hasBaseline ? (
          <span className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
            <span
              aria-hidden
              className="size-[7px] rounded-full"
              style={{ backgroundColor: CHART_PALETTE.slate }}
            />
            Baseline
          </span>
        ) : null}
        {hasPulsecover ? (
          <span className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
            <span
              aria-hidden
              className="size-[7px] rounded-full"
              style={{ backgroundColor: CHART_PALETTE.accent }}
            />
            PulseRecover
          </span>
        ) : null}
      </div>

      <div style={{ height }} className="w-full" role="img" aria-label={ariaLabel}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={data}
            margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
            barCategoryGap="32%"
            barGap={4}
          >
            <CartesianGrid {...cartesianGridProps} />
            <XAxis dataKey="label" {...xAxisProps} interval={0} />
            <YAxis
              domain={format === "percent" ? [0, 1] : undefined}
              tickFormatter={(v: number) => fmt(v)}
              {...yAxisProps}
            />
            <Tooltip
              {...tooltipProps}
              formatter={(value, name) => [
                fmt(Number(value)),
                name === "baseline" ? "Baseline" : "PulseRecover",
              ]}
            />
            {hasBaseline ? (
              <Bar
                dataKey="baseline"
                fill={CHART_PALETTE.slateFill}
                stroke={CHART_PALETTE.slate}
                strokeWidth={1}
                radius={[2, 2, 0, 0]}
                {...getAnimationProps(reduced)}
              />
            ) : null}
            {hasPulsecover ? (
              <Bar
                dataKey="pulsecover"
                fill={CHART_PALETTE.accentFill}
                stroke={CHART_PALETTE.accent}
                strokeWidth={1}
                radius={[2, 2, 0, 0]}
                {...getAnimationProps(reduced)}
              />
            ) : null}
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

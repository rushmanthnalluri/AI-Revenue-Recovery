/**
 * Metric vocabulary + formatting for the incident screens.
 *
 * Mirrors backend/app/services/detection/series.py: the detector runs over
 * `payment_success_rate` (0..1 ratio, degradation = down) and
 * `capture_latency_ms` (milliseconds, degradation = up). Unknown metrics fall
 * back to plain numbers so future detectors render without code changes.
 */

import { formatNumber, formatPercent } from "@/lib/format";

export const KNOWN_METRICS = ["payment_success_rate", "capture_latency_ms"] as const;

export type MetricDirection = "up" | "down";

/** Which movement is the degradation direction for a metric. */
export function metricDirection(metric: string): MetricDirection {
  if (metric === "capture_latency_ms") return "up";
  return "down"; // payment_success_rate and unknown rate-style metrics
}

/** True when metric values are 0..1 ratios rendered as percentages. */
export function isRatioMetric(metric: string): boolean {
  return metric === "payment_success_rate" || metric.includes("rate") || metric.includes("ratio");
}

/** Human label for a metric key: payment_success_rate → "Payment success rate". */
export function metricLabel(metric: string): string {
  const spaced = metric.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Format one metric value (baseline/observed/bucket) for its unit. */
export function formatMetricValue(metric: string, value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (isRatioMetric(metric)) return formatPercent(value, 1);
  if (metric.includes("latency")) return `${formatNumber(Math.round(value))} ms`;
  return formatNumber(value);
}

/** Signed deviation_pct (already a percentage number, e.g. -37.2 → "-37.2%"). */
export function formatDeviation(deviationPct: number | null | undefined): string {
  if (deviationPct === null || deviationPct === undefined || Number.isNaN(deviationPct)) return "—";
  const sign = deviationPct > 0 ? "+" : deviationPct < 0 ? "−" : "";
  return `${sign}${Math.abs(deviationPct).toFixed(1)}%`;
}

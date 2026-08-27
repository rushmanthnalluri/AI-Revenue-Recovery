/**
 * chartTheme — the recharts skin mandated by docs/ui-design-system.md
 * (ported from the Prop-pulse reference implementation). Plain objects meant
 * to be spread onto recharts components so every chart inherits the same
 * chrome: y-grid-only hairlines rgba(235,236,232,0.07), 10px IBM Plex Mono
 * ticks #8A9189, axis lines off, tooltip = floating layer on #202521 with the
 * sanctioned float shadow, 450ms ease-out animation (off under
 * prefers-reduced-motion).
 */

export const MONO_FONT = "var(--font-plex-mono), ui-monospace, 'SF Mono', monospace";

export const CHART_PALETTE = {
  accent: "#D9A63F",
  accentFill: "rgba(217, 166, 63, 0.55)",
  slate: "#6E8FA0",
  slateFill: "rgba(110, 143, 160, 0.45)",
  neutral: "#8A9189",
  neutralFill: "rgba(138, 145, 137, 0.4)",
  danger: "#D36B62",
  dangerFill: "rgba(211, 107, 98, 0.55)",
  grid: "rgba(235, 236, 232, 0.07)",
  tick: "#8A9189",
  text: "#EFF0EB",
  text2: "#A2A8A0",
  tooltipBg: "#202521",
  tooltipBorder: "#3A403B",
} as const;

/** Tick text style shared by both axes. */
export const axisTick = {
  fill: CHART_PALETTE.tick,
  fontSize: 10,
  fontFamily: MONO_FONT,
} as const;

/** Y-only hairline grid; x grid off. */
export const cartesianGridProps = {
  stroke: CHART_PALETTE.grid,
  vertical: false,
} as const;

export const xAxisProps = {
  tick: axisTick,
  axisLine: false,
  tickLine: false,
  tickMargin: 8,
} as const;

export const yAxisProps = {
  tick: axisTick,
  axisLine: false,
  tickLine: false,
  tickMargin: 6,
  width: 48,
} as const;

/** Tooltip chrome: floating layer — elevated panel, hairline border, radius
    8, the one sanctioned float shadow, mono 12px. */
export const tooltipProps = {
  contentStyle: {
    backgroundColor: CHART_PALETTE.tooltipBg,
    border: `1px solid ${CHART_PALETTE.tooltipBorder}`,
    borderRadius: 8,
    padding: "10px 12px",
    fontSize: 12,
    fontFamily: MONO_FONT,
    boxShadow: "0 4px 20px rgba(0, 0, 0, 0.5), 0 1px 3px rgba(0, 0, 0, 0.4)",
  },
  labelStyle: { color: CHART_PALETTE.text, fontWeight: 600, marginBottom: 4 },
  itemStyle: { color: CHART_PALETTE.text2, padding: 0 },
  cursor: { fill: "rgba(235, 236, 232, 0.05)" },
} as const;

export const CHART_ANIMATION_MS = 450;

/** Animation spread for series components (Area, Bar, Line…). */
export function getAnimationProps(reduced: boolean) {
  return {
    isAnimationActive: !reduced,
    animationDuration: reduced ? 0 : CHART_ANIMATION_MS,
    animationEasing: "ease-out" as const,
  };
}

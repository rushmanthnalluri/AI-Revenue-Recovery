/**
 * Display formatting. Money is integer paise (INR) everywhere in the API;
 * format it to ₹ via Intl.NumberFormat en-IN. Metrics use tabular-nums.
 */

const inr = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

const inrCompact = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  notation: "compact",
  maximumFractionDigits: 1,
});

const number = new Intl.NumberFormat("en-IN");

const dateTime = new Intl.DateTimeFormat("en-IN", {
  dateStyle: "medium",
  timeStyle: "short",
});

const timeOnly = new Intl.DateTimeFormat("en-IN", {
  hour: "2-digit",
  minute: "2-digit",
});

/** paise → "₹1,23,456". Compact → "₹1.2L" (en-IN lakh/crore grouping). */
export function formatINR(paise: number, opts: { compact?: boolean } = {}): string {
  const rupees = paise / 100;
  return (opts.compact ? inrCompact : inr).format(rupees);
}

/** 0..1 ratio → "94.2%". Returns the em dash for missing values. */
export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

/** Signed percentage-point delta → "+2.4 pp" / "-1.1 pp". */
export function formatDeltaPP(delta: number, digits = 1): string {
  const sign = delta > 0 ? "+" : delta < 0 ? "−" : "";
  return `${sign}${Math.abs(delta * 100).toFixed(digits)} pp`;
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return number.format(value);
}

export function formatMinutes(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (value < 1) return `${Math.round(value * 60)}s`;
  if (value < 60) return `${value.toFixed(1)}m`;
  return `${(value / 60).toFixed(1)}h`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return dateTime.format(d);
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return timeOnly.format(d);
}

/** Compact relative time: "just now", "5m ago", "2h ago", "3d ago". */
export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
  if (seconds < 45) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return dateTime.format(d);
}

import * as React from "react";
import { ShieldCheck } from "lucide-react";

import { Badge, type BadgeProps } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

type Tone = "success" | "warning" | "danger" | "info" | "neutral";

/**
 * Status vocabulary for the whole console. Maps every IncidentStatus,
 * RecoveryStatus, Severity, PolicyOutcome and health-check status onto the
 * spec palette: green ok / amber warn / red crit / slate in-progress /
 * neutral. Unknown statuses fall back to neutral (never crash on a status
 * the UI predates).
 */
const STATUS_TONES: Record<string, Tone> = {
  // health
  ok: "success",
  healthy: "success",
  live: "success",
  degraded: "warning",
  down: "danger",
  unreachable: "danger",
  error: "danger",
  disabled: "neutral", // deliberate configuration (e.g. WORKER_ENABLED=false), not a failure
  // incident status
  OPEN: "danger",
  INVESTIGATING: "info",
  DIAGNOSED: "info",
  RECOVERING: "info",
  RESOLVED: "success",
  CLOSED: "neutral",
  FALSE_POSITIVE: "neutral",
  // recovery status
  PROPOSED: "neutral",
  POLICY_EVALUATED: "neutral",
  PENDING_APPROVAL: "warning",
  APPROVED: "success",
  SCHEDULED: "info", // delayed retry parked until due; the worker fires it — pre-execution
  REJECTED: "neutral",
  EXECUTING: "info",
  VERIFYING: "info",
  RECOVERED: "success",
  FAILED: "danger",
  UNKNOWN: "warning",
  CANCELLED: "neutral",
  ESCALATED: "warning",
  // policy outcome (gate verdicts, backtest original/replayed)
  ALLOWED: "success",
  BLOCKED: "danger",
  REQUIRES_APPROVAL: "warning",
  // severity
  LOW: "neutral",
  MEDIUM: "warning",
  HIGH: "danger",
  CRITICAL: "danger",
  // evaluation run status
  completed: "success",
  running: "info",
  failed: "danger",
  pending: "neutral",
  // payment status (Razorpay lifecycle)
  captured: "success",
  authorized: "info",
  created: "neutral",
  refunded: "warning",
  partially_refunded: "warning",
};

const TONE_VARIANT: Record<Tone, BadgeProps["variant"]> = {
  success: "success",
  warning: "warning",
  danger: "danger",
  info: "info",
  neutral: "secondary",
};

const TONE_DOT: Record<Tone, string> = {
  success: "bg-success",
  warning: "bg-accent",
  danger: "bg-danger",
  info: "bg-info",
  neutral: "bg-text-3",
};

interface StatusPillProps {
  status: string;
  /** Show the colored dot before the label (default true). */
  dot?: boolean;
  /** Pulse the dot (for live/in-progress states). */
  pulse?: boolean;
  className?: string;
}

export function StatusPill({ status, dot = true, pulse = false, className }: StatusPillProps) {
  const tone = STATUS_TONES[status] ?? "neutral";
  // RECOVERED is verification-sourced (webhook/inline verify only) — it gets
  // a shield-check instead of the plain dot so "money recovered" can never be
  // confused with "action attempted" (EXECUTING/VERIFYING keep the info dot).
  const isVerifiedRecovered = status === "RECOVERED";
  return (
    <Badge variant={TONE_VARIANT[tone]} className={cn("normal-case", className)}>
      {isVerifiedRecovered ? (
        <ShieldCheck aria-hidden className="size-3" strokeWidth={1.5} />
      ) : dot ? (
        <span
          aria-hidden
          className={cn("size-[7px] rounded-full", TONE_DOT[tone], pulse && "animate-status-pulse")}
        />
      ) : null}
      {status.replace(/_/g, " ")}
    </Badge>
  );
}

import {
  ArrowUpRight,
  Ban,
  Bot,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  CircleDot,
  CircleHelp,
  CirclePlus,
  CircleX,
  HandCoins,
  Hourglass,
  ListTree,
  Play,
  RotateCcw,
  ScanSearch,
  SearchCheck,
  ShieldAlert,
  ShieldCheck,
  Siren,
  TrendingDown,
  Webhook,
  type LucideIcon,
} from "lucide-react";

/**
 * Audit action vocabulary → icon / tone / human label.
 *
 * The action strings mirror what the backend writes into `audit_logs`
 * (see backend/app/services/policy/audit.py callers): dotted names like
 * `recovery.action.approved`, webhook `verify_<status>` rows, and bare
 * detection verbs (`created` / `updated`) on incident rows. Anything
 * unrecognised falls back to a neutral dot and a humanized label, so new
 * backend actions still render readably.
 */

export type AuditTone = "success" | "warning" | "danger" | "info" | "neutral";

export interface AuditActionMeta {
  label: string;
  icon: LucideIcon;
  tone: AuditTone;
}

/** Recovery/status suffixes used by `recovery.action.*` and `recovery.opportunity.*`. */
const STATUS_META: Record<string, { label: string; icon: LucideIcon; tone: AuditTone }> = {
  proposed: { label: "Proposed", icon: CircleDashed, tone: "neutral" },
  policy_evaluated: { label: "Policy evaluated", icon: ShieldCheck, tone: "info" },
  pending_approval: { label: "Awaiting approval", icon: Hourglass, tone: "warning" },
  approved: { label: "Approved", icon: CircleCheck, tone: "success" },
  rejected: { label: "Rejected", icon: CircleX, tone: "danger" },
  executing: { label: "Executing", icon: Play, tone: "info" },
  verifying: { label: "Verifying", icon: SearchCheck, tone: "info" },
  recovered: { label: "Revenue recovered", icon: HandCoins, tone: "success" },
  failed: { label: "Failed", icon: CircleAlert, tone: "danger" },
  unknown: { label: "Outcome unknown", icon: CircleHelp, tone: "warning" },
  cancelled: { label: "Cancelled", icon: Ban, tone: "neutral" },
  escalated: { label: "Escalated", icon: ArrowUpRight, tone: "warning" },
};

const EXACT_META: Record<string, AuditActionMeta> = {
  "agent.investigate": { label: "AI investigation", icon: ScanSearch, tone: "info" },
  "agent.investigate_failed": {
    label: "AI investigation failed",
    icon: ScanSearch,
    tone: "danger",
  },
  "agent.action_requested": { label: "Agent action requested", icon: Bot, tone: "info" },
  "policy.action_blocked": {
    label: "Blocked by policy gate",
    icon: ShieldAlert,
    tone: "danger",
  },
  "recovery.approve": { label: "Recovery approved", icon: CircleCheck, tone: "success" },
  "recovery.opportunity_created": {
    label: "Opportunity created",
    icon: CirclePlus,
    tone: "neutral",
  },
  "recovery.strategies_generated": {
    label: "Strategies generated",
    icon: ListTree,
    tone: "neutral",
  },
  "recovery.action.proposed": { label: "Action proposed", icon: CircleDashed, tone: "neutral" },
  "recovery.action.resolve_check": {
    label: "Resolution check",
    icon: SearchCheck,
    tone: "info",
  },
  "incident.revenue_at_risk_refreshed": {
    label: "Revenue at risk refreshed",
    icon: TrendingDown,
    tone: "warning",
  },
  "demo.reset": { label: "Research dataset reset", icon: RotateCcw, tone: "warning" },
};

function humanize(raw: string): string {
  const last = raw.includes(".") ? raw.slice(raw.lastIndexOf(".") + 1) : raw;
  const words = last.replace(/_/g, " ").trim();
  return words.length > 0 ? words[0]!.toUpperCase() + words.slice(1) : raw;
}

/** Resolve an audit row to its icon, tone, and display label. */
export function auditActionMeta(action: string, entityType: string): AuditActionMeta {
  // Webhook verification rows: verify_recovered / verify_failed / verify_unknown.
  if (action.startsWith("verify_")) {
    const status = action.slice("verify_".length);
    const tone: AuditTone =
      status === "recovered" ? "success" : status === "failed" ? "danger" : "warning";
    const label =
      status === "recovered"
        ? "Webhook verified — recovered"
        : `Webhook verified — ${humanize(status).toLowerCase()}`;
    return { label, icon: Webhook, tone };
  }

  const exact = EXACT_META[action];
  if (exact) return exact;

  // recovery.action.<status> / recovery.opportunity.<status>
  const parts = action.split(".");
  const suffix = parts[parts.length - 1] ?? "";
  const statusMeta = STATUS_META[suffix];
  if (
    statusMeta &&
    (action.startsWith("recovery.action.") || action.startsWith("recovery.opportunity."))
  ) {
    const subject = action.startsWith("recovery.action.") ? "Action" : "Opportunity";
    return { ...statusMeta, label: `${subject} ${statusMeta.label.toLowerCase()}` };
  }

  // Detection writes bare verbs on incident rows.
  if (entityType === "incident") {
    if (action === "created") return { label: "Incident detected", icon: Siren, tone: "danger" };
    if (action === "updated") return { label: "Incident updated", icon: Siren, tone: "warning" };
    return { label: humanize(action), icon: Siren, tone: "neutral" };
  }

  if (entityType === "diagnosis") {
    return { label: humanize(action), icon: ScanSearch, tone: "info" };
  }

  return { label: humanize(action), icon: CircleDot, tone: "neutral" };
}

/** Tailwind classes for the timeline icon tile, per tone. */
export const AUDIT_TONE_TILE: Record<AuditTone, string> = {
  success: "border-transparent bg-success-dim text-success",
  warning: "border-transparent bg-accent-dim text-accent",
  danger: "border-transparent bg-danger-dim text-danger",
  info: "border-transparent bg-info-dim text-info",
  neutral: "border-border-strong bg-transparent text-text-3",
};

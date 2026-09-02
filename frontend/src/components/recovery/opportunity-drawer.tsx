"use client";

import * as React from "react";
import Link from "next/link";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Radar, X } from "lucide-react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, toApiError } from "@/lib/api";
import { formatDateTime, formatINR, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ErrorPanel } from "@/components/error-panel";
import { MetricStrip } from "@/components/metric-strip";
import { EnvironmentBadge } from "@/components/provenance";
import { StatusPill } from "@/components/status-pill";
import { Timeline, type TimelineItem } from "@/components/timeline";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  actionStateNote,
  actionTypeLabel,
  detailFailureClass,
  failureClassLabel,
  latestAction,
  opportunityTypeLabel,
  parseActionResponse,
  parseOpportunityDetail,
  type ActionResponseView,
  type OpportunityDetailView,
  type RecoveryActionItem,
} from "@/components/recovery/recovery-contract";
import { CONSOLE_ACTOR, useInvalidateRecovery, useModalA11y } from "@/components/recovery/recovery-hooks";
import { StrategyPanel } from "@/components/recovery/strategy-panel";

export function useOpportunityDetail(opportunityId: string, refetchInterval?: number) {
  return useQuery({
    queryKey: ["recovery", "detail", opportunityId],
    queryFn: () => api.recovery.get(opportunityId).then(parseOpportunityDetail),
    refetchInterval,
  });
}

// ---------------------------------------------------------------------------
// single action card (history + terminal outcomes + UNKNOWN resolution)
// ---------------------------------------------------------------------------

function TimestampGrid({ action }: { action: RecoveryActionItem }) {
  const stamps: [string, string | null | undefined][] = [
    ["Proposed", action.proposed_at],
    ["Executed", action.executed_at],
    ["Verified", action.verified_at],
    ["Completed", action.completed_at],
  ];
  return (
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {stamps.map(([label, value]) => (
        <div key={label}>
          <dt className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
            {label}
          </dt>
          <dd className="mt-0.5 font-mono text-2xs tabular-nums text-text-2">
            {value ? formatDateTime(value) : "—"}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function ActionCard({ action }: { action: RecoveryActionItem }) {
  const invalidate = useInvalidateRecovery();
  const [result, setResult] = React.useState<ActionResponseView | null>(null);

  const isUnknown = action.status === "UNKNOWN";

  /**
   * UNKNOWN resolution: the executor resolves ambiguity by re-querying gateway
   * truth (read-only GETs — never a blind re-fire). The backend exposes that
   * path through POST /recovery/{id}/execute: when the open action is UNKNOWN
   * the executor short-circuits into resolve() instead of executing.
   */
  const resolve = useMutation({
    mutationFn: () =>
      api.recovery
        .execute(action.opportunity_id, { actor: CONSOLE_ACTOR })
        .then(parseActionResponse),
    onSuccess: (data) => {
      setResult(data);
      invalidate();
    },
  });

  return (
    <article
      className={cn(
        "rounded-lg border px-4 py-3.5",
        isUnknown ? "border-accent-border bg-accent-wash" : "border-border bg-surface",
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-[13px] font-medium text-text">
            {actionTypeLabel(action.action_type)}
          </p>
          <StatusPill status={action.status} pulse={isUnknown} />
          {isUnknown ? <Badge variant="warning">Needs resolution</Badge> : null}
        </div>
        <span className="font-mono text-xs tabular-nums text-text">
          {formatINR(action.amount_paise)}
        </span>
      </div>

      <p className="mt-1 font-mono text-2xs text-text-3">
        {action.id}
        {action.strategy_id ? ` · strategy ${action.strategy_id}` : ""} · actor {action.actor} ·{" "}
        {action.attempts} attempt{action.attempts === 1 ? "" : "s"}
        {action.approved_by ? ` · approved by ${action.approved_by}` : ""}
      </p>

      {actionStateNote(action.status) && !isUnknown ? (
        <p
          className={cn(
            "mt-2 text-xs",
            action.status === "RECOVERED" ? "text-success" : "text-text-3",
          )}
        >
          {actionStateNote(action.status)}
        </p>
      ) : null}

      {isUnknown ? (
        <div className="mt-2.5 space-y-2">
          <p className="text-xs text-text-2">
            The gateway gave no authoritative answer, so the payment may or may not have
            happened. Re-querying fetches the true state from the gateway (read-only) — the
            charge is never re-fired blindly.
          </p>
          {action.last_error ? (
            <p className="font-mono text-2xs text-danger">last error: {action.last_error}</p>
          ) : null}
          <div className="flex items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              disabled={resolve.isPending}
              onClick={() => resolve.mutate()}
            >
              <Radar aria-hidden />
              {resolve.isPending ? "Re-querying…" : "Re-query gateway truth"}
            </Button>
          </div>
          {resolve.isError ? (
            <p role="alert" className="text-xs text-danger">
              {toApiError(resolve.error).status
                ? `Re-query failed (${toApiError(resolve.error).status}): `
                : ""}
              {toApiError(resolve.error).message}
            </p>
          ) : null}
          {result ? (
            <p className="text-xs text-text-2">
              <StatusPill status={result.status} className="mr-2" />
              {result.message}
            </p>
          ) : null}
        </div>
      ) : null}

      {action.note && !isUnknown ? (
        <p className="mt-2 text-xs text-text-2">note: {action.note}</p>
      ) : null}
      {action.last_error && !isUnknown ? (
        <p className="mt-2 font-mono text-2xs text-danger">last error: {action.last_error}</p>
      ) : null}

      <div className="mt-3">
        <TimestampGrid action={action} />
      </div>

      {action.policy_decision ? (
        <div className="mt-3 rounded-md border border-border bg-bg px-3 py-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
              Policy gate
            </span>
            <Badge
              variant={
                action.policy_decision.outcome === "ALLOWED"
                  ? "success"
                  : action.policy_decision.outcome === "BLOCKED"
                    ? "danger"
                    : "warning"
              }
            >
              {action.policy_decision.outcome.replace(/_/g, " ")}
            </Badge>
            <span className="font-mono text-2xs text-text-3">
              v{action.policy_decision.policy_version} · {formatDateTime(action.policy_decision.decided_at)}
            </span>
          </div>
          {action.policy_decision.rules_matched.length > 0 ? (
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {action.policy_decision.rules_matched.map((rule) => (
                <span
                  key={rule}
                  className="rounded-sm border border-border-strong px-[7px] py-[3px] font-mono text-[10px] text-text-2"
                >
                  {rule}
                </span>
              ))}
            </div>
          ) : null}
          {action.policy_decision.reasons.length > 0 ? (
            <ul className="mt-1.5 list-inside list-disc space-y-0.5 text-xs text-text-2">
              {action.policy_decision.reasons.map((reason, i) => (
                <li key={i}>{reason}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {action.gateway_request_id ? (
        <p className="mt-2 font-mono text-2xs text-text-3">
          gateway request {action.gateway_request_id}
        </p>
      ) : null}
    </article>
  );
}

// ---------------------------------------------------------------------------
// audit trail
// ---------------------------------------------------------------------------

function auditTone(action: string): TimelineItem["tone"] {
  if (action.includes("reject") || action.includes("failed") || action.includes("blocked")) {
    return "danger";
  }
  if (action.includes("approv") || action.includes("recover") || action.includes("execut")) {
    return "success";
  }
  if (action.includes("escalat") || action.includes("unknown")) return "warning";
  return "info";
}

function AuditTrail({ detail }: { detail: OpportunityDetailView }) {
  const audit = detail.audit ?? [];
  if (audit.length === 0) return null;
  const items: TimelineItem[] = audit.map((row) => ({
    id: row.id,
    title: (
      <span className="font-mono text-xs">
        {row.action} <span className="text-text-3">· {row.actor}</span>
      </span>
    ),
    timestamp: row.created_at,
    tone: auditTone(row.action),
  }));
  return (
    <section aria-label="Audit trail">
      <h3 className="font-mono text-[11px] uppercase tracking-[0.11em] text-text-3">
        Audit trail
      </h3>
      <Timeline items={items} className="mt-3" />
    </section>
  );
}

// ---------------------------------------------------------------------------
// drawer body
// ---------------------------------------------------------------------------

function DrawerBody({ opportunityId }: { opportunityId: string }) {
  const detail = useOpportunityDetail(opportunityId);

  if (detail.isPending) {
    return (
      <div className="space-y-4 p-6" aria-busy="true" aria-label="Loading opportunity detail">
        <Skeleton className="h-6 w-2/3" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-44 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (detail.isError) {
    return (
      <div className="p-6">
        <ErrorPanel error={detail.error} onRetry={() => detail.refetch()} />
      </div>
    );
  }

  const opp = detail.data;
  const failureClass = detailFailureClass(opp);
  const actions = [...(opp.actions ?? [])].reverse(); // newest first
  const latest = latestAction(opp);

  return (
    <div className="space-y-6 p-6">
      {/* header */}
      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.11em] text-text-3">
          {opportunityTypeLabel(opp.opportunity_type)} · {opp.id}
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <StatusPill status={opp.status} />
          <EnvironmentBadge environment={opp.environment} />
          <Badge variant="outline">risk: {opp.risk}</Badge>
          {failureClass ? <Badge variant="accent">{failureClassLabel(failureClass)}</Badge> : null}
        </div>
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-2xs text-text-3">
          {opp.customer_id ? <span>customer {opp.customer_id}</span> : null}
          {opp.payment_id ? <span>payment {opp.payment_id}</span> : null}
          {opp.subscription_id ? <span>subscription {opp.subscription_id}</span> : null}
          {opp.incident_id ? (
            <Link
              href={`/incidents/${opp.incident_id}`}
              className="text-accent underline-offset-2 hover:underline"
            >
              incident {opp.incident_id}
            </Link>
          ) : null}
        </div>
      </div>

      <MetricStrip
        items={[
          {
            key: "amount",
            label: "Amount at stake",
            value: formatINR(opp.amount_paise),
            hint: `created ${formatDateTime(opp.created_at)}`,
          },
          {
            key: "expected",
            label: "Expected recovery",
            value: formatINR(opp.expected_recovery_paise),
            tone: "success",
            hint: opp.expires_at ? `expires ${formatDateTime(opp.expires_at)}` : undefined,
          },
          {
            key: "confidence",
            label: "Confidence",
            value: formatPercent(opp.confidence, 0),
            hint: "opportunity-level estimate",
          },
        ]}
      />

      {opp.reason ? <p className="text-[13px] text-text-2">{opp.reason}</p> : null}

      {/* strategy comparison */}
      <section aria-label="Strategy comparison">
        <h3 className="font-mono text-[11px] uppercase tracking-[0.11em] text-text-3">
          Strategy comparison
        </h3>
        <div className="mt-3">
          <StrategyPanel opportunityId={opp.id} amountPaise={opp.amount_paise} />
        </div>
      </section>

      {/* action history */}
      <section aria-label="Recovery actions">
        <h3 className="font-mono text-[11px] uppercase tracking-[0.11em] text-text-3">
          Actions {latest ? `· latest ${latest.status.replace(/_/g, " ")}` : ""}
        </h3>
        {actions.length === 0 ? (
          <p className="mt-3 rounded-lg border border-dashed border-border-strong px-4 py-6 text-center text-xs text-text-3">
            No recovery actions yet — executing a strategy creates the first one and runs it
            through the policy gate.
          </p>
        ) : (
          <div className="mt-3 space-y-3">
            {actions.map((action) => (
              <ActionCard key={action.id} action={action} />
            ))}
          </div>
        )}
      </section>

      <AuditTrail detail={opp} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// drawer shell
// ---------------------------------------------------------------------------

interface OpportunityDrawerProps {
  opportunityId: string | null;
  onClose: () => void;
}

/**
 * Opportunity detail drawer — a floating layer (bg-elevated + shadow-float,
 * the spec's one sanctioned shadow use) sliding in from the right.
 */
export function OpportunityDrawer({ opportunityId, onClose }: OpportunityDrawerProps) {
  return (
    <AnimatePresence>
      {opportunityId ? (
        <DrawerShell key={opportunityId} opportunityId={opportunityId} onClose={onClose} />
      ) : null}
    </AnimatePresence>
  );
}

function DrawerShell({
  opportunityId,
  onClose,
}: {
  opportunityId: string;
  onClose: () => void;
}) {
  const reduce = useReducedMotion();
  const panelRef = React.useRef<HTMLDivElement>(null);
  useModalA11y(panelRef, onClose);

  return (
    <motion.div
      className="fixed inset-0 z-50"
      initial={reduce ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.2 }}
    >
      <div aria-hidden className="absolute inset-0 bg-black/60" onClick={onClose} />
      <motion.div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Recovery opportunity ${opportunityId}`}
        tabIndex={-1}
        className="absolute inset-y-0 right-0 w-full max-w-[760px] overflow-y-auto border-l border-border-strong bg-elevated shadow-float"
        initial={reduce ? false : { x: 48 }}
        animate={{ x: 0 }}
        exit={reduce ? undefined : { x: 48 }}
        transition={{ duration: 0.45, ease: [0.32, 0.72, 0, 1] }}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-elevated/95 px-6 py-3 backdrop-blur-[8px]">
          <p className="font-mono text-[10px] uppercase tracking-[0.11em] text-text-3">
            Opportunity detail
          </p>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close opportunity detail"
            className="rounded-md p-1.5 text-text-3 transition-colors duration-150 ease-apple hover:bg-raised hover:text-text"
          >
            <X className="size-4" strokeWidth={1.5} aria-hidden />
          </button>
        </div>
        <DrawerBody opportunityId={opportunityId} />
      </motion.div>
    </motion.div>
  );
}

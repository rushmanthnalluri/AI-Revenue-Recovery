"use client";

import * as React from "react";
import { TriangleAlert } from "lucide-react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, toApiError } from "@/lib/api";
import type { OpportunitySummary } from "@/lib/types";
import { formatINR, formatNumber, timeAgo } from "@/lib/format";
import { ConfidenceBar } from "@/components/confidence-bar";
import { EmptyState } from "@/components/empty-state";
import { useEnvironment } from "@/components/environment-provider";
import { ErrorPanel } from "@/components/error-panel";
import { MetricStrip } from "@/components/metric-strip";
import { SectionCard } from "@/components/section-card";
import { StatusPill } from "@/components/status-pill";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  actionTypeLabel,
  latestAction,
  opportunityTypeLabel,
  parseActionResponse,
  type ActionResponseView,
  type PolicyDecisionItem,
  type RecoveryActionItem,
} from "@/components/recovery/recovery-contract";
import { CONSOLE_ACTOR, useInvalidateRecovery } from "@/components/recovery/recovery-hooks";
import { useOpportunityDetail } from "@/components/recovery/opportunity-drawer";

// ---------------------------------------------------------------------------
// policy evaluation summary (shared by pending + unknown cards)
// ---------------------------------------------------------------------------

function PolicySummary({ decision }: { decision: PolicyDecisionItem | null }) {
  if (!decision) {
    return (
      <p className="text-xs text-text-3">
        No persisted policy decision on the open action yet.
      </p>
    );
  }
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
          Policy gate
        </span>
        <Badge
          variant={
            decision.outcome === "ALLOWED"
              ? "success"
              : decision.outcome === "BLOCKED"
                ? "danger"
                : "warning"
          }
        >
          {decision.outcome.replace(/_/g, " ")}
        </Badge>
        <span className="font-mono text-2xs text-text-3">v{decision.policy_version}</span>
      </div>
      {decision.rules_matched.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {decision.rules_matched.map((rule) => (
            <span
              key={rule}
              className="rounded-sm border border-border-strong px-[7px] py-[3px] font-mono text-[10px] text-text-2"
            >
              {rule}
            </span>
          ))}
        </div>
      ) : null}
      {decision.reasons.length > 0 ? (
        <ul className="list-inside list-disc space-y-0.5 text-xs text-text-2">
          {decision.reasons.map((reason, i) => (
            <li key={i}>{reason}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// pending-approval card
// ---------------------------------------------------------------------------

function PendingApprovalCard({ opportunity }: { opportunity: OpportunitySummary }) {
  const detail = useOpportunityDetail(opportunity.id, 10_000);
  const invalidate = useInvalidateRecovery();
  const [note, setNote] = React.useState("");
  const [outcome, setOutcome] = React.useState<ActionResponseView | null>(null);

  const action: RecoveryActionItem | null =
    detail.data?.actions?.find((a) => a.status === "PENDING_APPROVAL") ??
    (detail.data ? latestAction(detail.data) : null);

  const onSettled = (result: ActionResponseView) => {
    setOutcome(result);
    setNote("");
    invalidate();
  };

  const approve = useMutation({
    mutationFn: () =>
      api.recovery
        .approve(opportunity.id, { actor: CONSOLE_ACTOR, note: note.trim() || null })
        .then(parseActionResponse),
    onSuccess: onSettled,
  });
  const reject = useMutation({
    mutationFn: () =>
      api.recovery
        .reject(opportunity.id, { actor: CONSOLE_ACTOR, reason: note.trim() })
        .then(parseActionResponse),
    onSuccess: onSettled,
  });
  const escalate = useMutation({
    mutationFn: () =>
      api.recovery
        .escalate(opportunity.id, { actor: CONSOLE_ACTOR, reason: note.trim() })
        .then(parseActionResponse),
    onSuccess: onSettled,
  });

  const busy = approve.isPending || reject.isPending || escalate.isPending;
  const mutationError = approve.error ?? reject.error ?? escalate.error;
  const noteMissing = note.trim().length === 0;

  return (
    <article className="rounded-lg border border-border bg-surface px-4 py-3.5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-[13px] font-medium text-text">
              {action ? actionTypeLabel(action.action_type) : opportunityTypeLabel(opportunity.opportunity_type)}
            </p>
            <StatusPill status={opportunity.status} />
          </div>
          <p className="mt-1 font-mono text-2xs text-text-3">
            {opportunity.id}
            {opportunity.customer_id ? ` · customer ${opportunity.customer_id}` : ""}
            {opportunity.payment_id ? ` · order/payment ${opportunity.payment_id}` : ""}
            {" · "}opened {timeAgo(opportunity.created_at)}
          </p>
        </div>
        <div className="text-right">
          <p className="font-mono text-sm tabular-nums text-text">
            {formatINR(action?.amount_paise ?? opportunity.amount_paise)}
          </p>
          <ConfidenceBar
            value={action?.confidence ?? opportunity.confidence}
            className="mt-1 justify-end"
          />
        </div>
      </div>

      {opportunity.reason ? (
        <p className="mt-2 text-xs text-text-2">{opportunity.reason}</p>
      ) : null}

      <div className="mt-2.5 rounded-md border border-border bg-bg px-3 py-2.5">
        {detail.isPending ? (
          <Skeleton className="h-8 w-full" />
        ) : detail.isError ? (
          <p role="alert" className="text-xs text-danger">
            Could not load the policy evaluation: {toApiError(detail.error).message}
          </p>
        ) : (
          <PolicySummary decision={action?.policy_decision ?? null} />
        )}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <Input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Decision note — required for reject / escalate"
          aria-label={`Decision note for ${opportunity.id}`}
          className="h-8 min-w-[220px] flex-1 text-xs"
          disabled={busy}
        />
        <Button
          variant="success"
          size="sm"
          disabled={busy}
          onClick={() => approve.mutate()}
        >
          {approve.isPending ? "Approving…" : "Approve"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={busy || noteMissing}
          title={noteMissing ? "Rejection requires a reason in the note field" : undefined}
          onClick={() => reject.mutate()}
        >
          {reject.isPending ? "Rejecting…" : "Reject"}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={busy || noteMissing}
          title={noteMissing ? "Escalation requires a reason in the note field" : undefined}
          onClick={() => escalate.mutate()}
        >
          {escalate.isPending ? "Escalating…" : "Escalate"}
        </Button>
      </div>

      {mutationError ? (
        <p role="alert" className="mt-2 text-xs text-danger">
          {toApiError(mutationError).status
            ? `Decision refused (${toApiError(mutationError).status}): `
            : ""}
          {toApiError(mutationError).message}
        </p>
      ) : null}
      {outcome ? (
        <p className="mt-2 text-xs text-text-2">
          <StatusPill status={outcome.status} className="mr-2" />
          {outcome.message}
        </p>
      ) : null}
    </article>
  );
}

// ---------------------------------------------------------------------------
// UNKNOWN ("needs resolution") card
// ---------------------------------------------------------------------------

function UnknownActionCard({ opportunity }: { opportunity: OpportunitySummary }) {
  const detail = useOpportunityDetail(opportunity.id, 15_000);
  const invalidate = useInvalidateRecovery();
  const [result, setResult] = React.useState<ActionResponseView | null>(null);

  const action =
    detail.data?.actions?.find((a) => a.status === "UNKNOWN") ??
    (detail.data ? latestAction(detail.data) : null);

  /**
   * The API exposes UNKNOWN resolution through POST /recovery/{id}/execute:
   * when the open action is UNKNOWN the executor never re-fires the mutation —
   * it re-queries gateway truth (read-only) and settles the action.
   */
  const resolve = useMutation({
    mutationFn: () =>
      api.recovery
        .execute(opportunity.id, { actor: CONSOLE_ACTOR })
        .then(parseActionResponse),
    onSuccess: (data) => {
      setResult(data);
      invalidate();
    },
  });

  return (
    <article className="rounded-lg border border-accent-border bg-accent-wash px-4 py-3.5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <TriangleAlert className="size-4 text-accent" strokeWidth={1.5} aria-hidden />
            <p className="text-[13px] font-medium text-text">
              {action ? actionTypeLabel(action.action_type) : opportunityTypeLabel(opportunity.opportunity_type)}
            </p>
            <StatusPill status="UNKNOWN" pulse />
            <Badge variant="warning">Needs resolution</Badge>
          </div>
          <p className="mt-1 font-mono text-2xs text-text-3">
            {opportunity.id}
            {opportunity.customer_id ? ` · customer ${opportunity.customer_id}` : ""}
            {opportunity.payment_id ? ` · order/payment ${opportunity.payment_id}` : ""}
            {" · "}opened {timeAgo(opportunity.created_at)}
          </p>
        </div>
        <p className="font-mono text-sm tabular-nums text-text">
          {formatINR(action?.amount_paise ?? opportunity.amount_paise)}
        </p>
      </div>

      <p className="mt-2 text-xs text-text-2">
        The gateway gave no authoritative answer for this charge, so the outcome is
        ambiguous. Re-querying fetches the true payment state from the gateway (read-only
        GETs) — the mutation is never re-fired blindly.
        {action?.last_error ? (
          <span className="mt-1 block font-mono text-2xs text-danger">
            last error: {action.last_error}
          </span>
        ) : null}
      </p>

      <div className="mt-3 flex items-center gap-2">
        <Button
          variant="secondary"
          size="sm"
          disabled={resolve.isPending}
          onClick={() => resolve.mutate()}
        >
          {resolve.isPending ? "Re-querying…" : "Re-query gateway truth"}
        </Button>
      </div>

      {resolve.isError ? (
        <p role="alert" className="mt-2 text-xs text-danger">
          {toApiError(resolve.error).status
            ? `Re-query failed (${toApiError(resolve.error).status}): `
            : ""}
          {toApiError(resolve.error).message}
        </p>
      ) : null}
      {result ? (
        <p className="mt-2 text-xs text-text-2">
          <StatusPill status={result.status} className="mr-2" />
          {result.message}
        </p>
      ) : null}
    </article>
  );
}

// ---------------------------------------------------------------------------
// approval center panel
// ---------------------------------------------------------------------------

export function ApprovalsPanel() {
  const { environment } = useEnvironment();
  const pending = useQuery({
    queryKey: ["recovery", "opportunities", "pending-approval", environment],
    queryFn: () =>
      api.recovery.opportunities({
        status: "PENDING_APPROVAL",
        page: 1,
        page_size: 50,
        environment,
      }),
    refetchInterval: 10_000,
  });
  const unknown = useQuery({
    queryKey: ["recovery", "opportunities", "unknown", environment],
    queryFn: () =>
      api.recovery.opportunities({ status: "UNKNOWN", page: 1, page_size: 50, environment }),
    refetchInterval: 15_000,
  });
  // Whole-queue aggregate (SQL COUNT/SUM over the ENTIRE pending-approval
  // lane) — the correct queue value beyond page 1 of the list.
  const summary = useQuery({
    queryKey: ["recovery", "approvals-summary", environment],
    queryFn: () => api.recovery.approvalsSummary(environment),
    refetchInterval: 10_000,
  });

  const pendingItems = pending.data?.items ?? [];
  const unknownItems = unknown.data?.items ?? [];
  const showUnknownSection = unknown.isPending || unknown.isError || unknownItems.length > 0;

  return (
    <div className="space-y-6">
      <MetricStrip
        items={[
          {
            key: "pending",
            label: "Awaiting decision",
            value: pending.data ? formatNumber(pending.data.total) : "—",
            tone: pending.data && pending.data.total > 0 ? "warning" : "default",
            loading: pending.isPending,
            hint: "policy gate returned REQUIRES_APPROVAL",
          },
          {
            key: "pending-value",
            label: "Value awaiting decision",
            value: summary.data ? formatINR(summary.data.pending_amount_paise) : "—",
            loading: summary.isPending,
            hint: "summed across the entire pending queue",
          },
          {
            key: "unknown",
            label: "Needs resolution",
            value: unknown.data ? formatNumber(unknown.data.total) : "—",
            tone: unknown.data && unknown.data.total > 0 ? "warning" : "default",
            loading: unknown.isPending,
            hint: "ambiguous gateway outcomes (UNKNOWN)",
          },
        ]}
      />

      <SectionCard
        title="Awaiting decision"
        description="The deterministic policy gate refused to auto-execute these — a human decision is required before anything fires."
        contentClassName="space-y-3 pt-0"
      >
        {pending.isError ? (
          <ErrorPanel error={pending.error} onRetry={() => pending.refetch()} />
        ) : pending.isPending ? (
          <div className="space-y-3" aria-busy="true" aria-label="Loading approval queue">
            <Skeleton className="h-32 w-full" />
            <Skeleton className="h-32 w-full" />
          </div>
        ) : pendingItems.length === 0 ? (
          <EmptyState
            title="Approval queue is clear"
            description={
              environment === "real_test"
                ? "Nothing is waiting on a human decision. Items land here when the policy gate returns REQUIRES_APPROVAL on your observed Razorpay Test Mode activity."
                : "Nothing is waiting on a human decision. Items land here when the policy gate returns REQUIRES_APPROVAL — run a scenario from the Research Lab and build opportunities from an incident to see the flow."
            }
          />
        ) : (
          pendingItems.map((opportunity) => (
            <PendingApprovalCard key={opportunity.id} opportunity={opportunity} />
          ))
        )}
      </SectionCard>

      {showUnknownSection ? (
        <SectionCard
          title="Needs resolution — ambiguous outcomes"
          description="UNKNOWN means the gateway never gave an authoritative answer. Resolve by re-querying truth; never by re-firing the charge."
          contentClassName="space-y-3 pt-0"
        >
          {unknown.isError ? (
            <ErrorPanel error={unknown.error} onRetry={() => unknown.refetch()} />
          ) : unknown.isPending ? (
            <Skeleton className="h-28 w-full" aria-busy="true" aria-label="Loading ambiguous outcomes" />
          ) : (
            unknownItems.map((opportunity) => (
              <UnknownActionCard key={opportunity.id} opportunity={opportunity} />
            ))
          )}
        </SectionCard>
      ) : null}
    </div>
  );
}

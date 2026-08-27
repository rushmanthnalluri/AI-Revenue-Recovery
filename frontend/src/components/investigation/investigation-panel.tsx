"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BrainCircuit,
  ClipboardCheck,
  Eye,
  Loader2,
  RefreshCw,
  ScanSearch,
  ShieldAlert,
  TriangleAlert,
} from "lucide-react";

import { ApiError, api } from "@/lib/api";
import { formatDateTime, formatINR, formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfidenceBar } from "@/components/confidence-bar";
import { EmptyState } from "@/components/empty-state";
import { ErrorPanel } from "@/components/error-panel";
import { SectionCard } from "@/components/section-card";
import {
  parseInvestigateResponse,
  parseInvestigationReport,
  shortRef,
  type AiInference,
  type InvestigationReport,
  type ObservedFact,
  type PolicyOutcomeValue,
  type RecommendedAction,
} from "@/components/investigation/investigation-types";

const POLL_INTERVAL_MS = 2_500;
const POLL_TIMEOUT_MS = 120_000;

/* ------------------------------------------------------------------ */
/* small pieces                                                        */
/* ------------------------------------------------------------------ */

/** Mono id chip used for evidence references and fact cross-references. */
function RefChip({ id, prefix }: { id: string; prefix: string }) {
  return (
    <span
      title={id}
      className="rounded-sm border border-border-strong px-[5px] py-[1px] font-mono text-[9px] tabular-nums text-text-3"
    >
      {prefix}:{shortRef(id)}
    </span>
  );
}

const POLICY_VARIANT: Record<string, "success" | "warning" | "danger" | "secondary"> = {
  ALLOWED: "success",
  REQUIRES_APPROVAL: "warning",
  BLOCKED: "danger",
};

function PolicyOutcomePill({ outcome }: { outcome: PolicyOutcomeValue }) {
  return (
    <Badge variant={POLICY_VARIANT[outcome] ?? "secondary"}>
      policy: {outcome.replace(/_/g, " ")}
    </Badge>
  );
}

/* ------------------------------------------------------------------ */
/* zone 1 — observed facts (neutral slate)                             */
/* ------------------------------------------------------------------ */

function FactRow({ fact }: { fact: ObservedFact }) {
  const hasData = Object.keys(fact.data).length > 0;
  return (
    <li className="py-2.5 first:pt-0 last:pb-0">
      <p className="text-[13px] leading-relaxed text-text">{fact.statement}</p>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        <span className="rounded-sm bg-info-dim px-[5px] py-[1px] font-mono text-[9px] uppercase tracking-[0.07em] text-info">
          {fact.tool}
        </span>
        {fact.evidence_ids.map((id) => (
          <RefChip key={id} id={id} prefix="ev" />
        ))}
        {hasData ? (
          <details className="group w-full">
            <summary className="cursor-pointer select-none font-mono text-[10px] uppercase tracking-[0.07em] text-text-3 transition-colors hover:text-text-2">
              raw tool output
            </summary>
            <pre className="mt-1.5 max-h-48 overflow-auto rounded-md border border-border bg-bg p-2.5 font-mono text-[11px] leading-relaxed text-text-2">
              {JSON.stringify(fact.data, null, 2)}
            </pre>
          </details>
        ) : null}
      </div>
    </li>
  );
}

/* ------------------------------------------------------------------ */
/* zone 2 — AI inference (amber)                                       */
/* ------------------------------------------------------------------ */

function InferenceRow({ inference }: { inference: AiInference }) {
  return (
    <li className="py-2.5 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <p className="min-w-0 flex-1 text-[13px] leading-relaxed text-text">
          {inference.statement}
        </p>
        <ConfidenceBar value={inference.confidence} className="mt-0.5 shrink-0" />
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        {inference.label && inference.label !== "inference" ? (
          <Badge variant="warning">{inference.label.replace(/_/g, " ")}</Badge>
        ) : null}
        {inference.supporting_fact_ids.map((id) => (
          <RefChip key={id} id={id} prefix="fact" />
        ))}
      </div>
    </li>
  );
}

/* ------------------------------------------------------------------ */
/* zone 3 — recommended action (policy-gated)                          */
/* ------------------------------------------------------------------ */

function ActionCard({ action, primary }: { action: RecommendedAction; primary?: boolean }) {
  return (
    <div
      className={cn(
        "rounded-md border px-3.5 py-3",
        primary ? "border-border-strong bg-raised" : "border-border bg-surface",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="accent">{action.action_type.replace(/_/g, " ")}</Badge>
        {primary ? <Badge variant="outline">next step</Badge> : null}
        {action.policy_preview ? (
          <PolicyOutcomePill outcome={action.policy_preview.outcome} />
        ) : (
          <Badge variant="secondary">policy: not evaluated</Badge>
        )}
      </div>
      <p className="mt-2 text-[13px] leading-relaxed text-text-2">{action.rationale}</p>
      <dl className="mt-2 flex flex-wrap gap-x-6 gap-y-1 font-mono text-2xs tabular-nums text-text-3">
        {action.amount_paise !== null ? (
          <div className="flex gap-1.5">
            <dt className="uppercase tracking-[0.07em]">amount</dt>
            <dd className="text-text-2">{formatINR(action.amount_paise)}</dd>
          </div>
        ) : null}
        {action.expected_recovery_paise !== null ? (
          <div className="flex gap-1.5">
            <dt className="uppercase tracking-[0.07em]">expected recovery</dt>
            <dd className="text-text-2">{formatINR(action.expected_recovery_paise)}</dd>
          </div>
        ) : null}
        {action.opportunity_id ? (
          <div className="flex gap-1.5">
            <dt className="uppercase tracking-[0.07em]">opportunity</dt>
            <dd className="text-text-2">{shortRef(action.opportunity_id)}</dd>
          </div>
        ) : null}
      </dl>
      {action.policy_preview &&
      (action.policy_preview.reasons.length > 0 ||
        action.policy_preview.rules_matched.length > 0) ? (
        <div className="mt-2 border-t border-border pt-2">
          {action.policy_preview.rules_matched.length > 0 ? (
            <p className="font-mono text-2xs text-text-3">
              rules: {action.policy_preview.rules_matched.join(", ")} · policy v
              {action.policy_preview.policy_version}
            </p>
          ) : null}
          <ul className="mt-1 space-y-0.5">
            {action.policy_preview.reasons.map((reason, i) => (
              <li key={i} className="text-xs text-text-3">
                · {reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* zone frame                                                          */
/* ------------------------------------------------------------------ */

function Zone({
  icon: Icon,
  kicker,
  tone,
  children,
}: {
  icon: React.ComponentType<{ className?: string; strokeWidth?: number }>;
  kicker: string;
  tone: "slate" | "amber" | "plain";
  children: React.ReactNode;
}) {
  return (
    <section
      aria-label={kicker}
      className={cn(
        "rounded-md border px-4 py-3.5",
        tone === "slate" && "border-border bg-raised",
        tone === "amber" && "border-accent-border bg-accent-wash",
        tone === "plain" && "border-border bg-surface",
      )}
    >
      <h4
        className={cn(
          "flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.11em]",
          tone === "slate" && "text-info",
          tone === "amber" && "text-accent",
          tone === "plain" && "text-text-2",
        )}
      >
        <Icon className="size-3.5" strokeWidth={1.5} aria-hidden />
        {kicker}
      </h4>
      <div className="mt-3">{children}</div>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* the report                                                          */
/* ------------------------------------------------------------------ */

function InvestigationReportView({ report }: { report: InvestigationReport }) {
  const llm = report.reasoner !== "heuristic";
  /* The next step may also appear in recommended_actions — drop only the
     exact duplicate (same type + target + amount), never siblings that share
     an action_type for different payments. */
  const next = report.recommended_next_step;
  const actions: RecommendedAction[] = [
    ...(next ? [next] : []),
    ...report.recommended_actions.filter(
      (a) =>
        !next ||
        a.action_type !== next.action_type ||
        a.payment_id !== next.payment_id ||
        a.opportunity_id !== next.opportunity_id ||
        a.amount_paise !== next.amount_paise,
    ),
  ];

  return (
    <div className="space-y-4">
      {/* honesty badges */}
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={llm ? "info" : "warning"}>
          {llm ? `LLM reasoner · ${report.generated_by}` : "heuristic reasoner"}
        </Badge>
        {report.degraded ? (
          <Badge variant="danger">
            <TriangleAlert className="size-3" strokeWidth={1.5} aria-hidden />
            degraded
          </Badge>
        ) : null}
        {report.escalated ? (
          <Badge variant="warning">
            <ShieldAlert className="size-3" strokeWidth={1.5} aria-hidden />
            escalated
          </Badge>
        ) : null}
        <span className="ml-auto">
          <ConfidenceBar value={report.confidence} />
        </span>
      </div>

      {report.escalated ? (
        <div
          role="alert"
          className="rounded-lg border border-accent-border bg-accent-wash px-4 py-3.5"
        >
          <p className="flex items-center gap-2 text-[13.5px] font-semibold text-accent">
            <ShieldAlert className="size-4" strokeWidth={1.5} aria-hidden />
            Escalation required — a human must review before any action
          </p>
          <ul className="mt-1.5 space-y-0.5">
            {report.escalation_reasons.map((reason, i) => (
              <li key={i} className="text-xs text-text-2">
                · {reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {report.degraded && report.degraded_reasons.length > 0 ? (
        <p className="text-xs text-text-3">
          Degraded because: {report.degraded_reasons.join("; ")}.
        </p>
      ) : null}

      {report.summary ? (
        <p className="text-sm leading-relaxed text-text-2">{report.summary}</p>
      ) : null}

      {/* zone 1 — observed facts */}
      <Zone icon={Eye} kicker="Observed facts · deterministic tool output" tone="slate">
        {report.observed_facts.length === 0 ? (
          <p className="text-xs text-text-3">No tool-derived facts were recorded.</p>
        ) : (
          <ul className="divide-y divide-border">
            {report.observed_facts.map((fact) => (
              <FactRow key={fact.id || fact.statement} fact={fact} />
            ))}
          </ul>
        )}
      </Zone>

      {/* zone 2 — AI inference */}
      <Zone icon={BrainCircuit} kicker="AI inference · probabilistic, not evidence" tone="amber">
        {report.ai_inferences.length === 0 ? (
          <p className="text-xs text-text-3">The reasoner produced no inferences.</p>
        ) : (
          <ul className="divide-y divide-border">
            {report.ai_inferences.map((inference) => (
              <InferenceRow key={inference.id || inference.statement} inference={inference} />
            ))}
          </ul>
        )}
        {report.alternative_hypotheses.length > 0 ? (
          <div className="mt-3 border-t border-accent-border pt-3">
            <h5 className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
              alternative hypotheses
            </h5>
            <ul className="mt-2 space-y-1.5">
              {report.alternative_hypotheses.map((hyp) => (
                <li key={`${hyp.rank}-${hyp.cause}`} className="flex items-center justify-between gap-3">
                  <span className="flex min-w-0 items-baseline gap-2">
                    <span className="font-mono text-2xs tabular-nums text-text-3">#{hyp.rank}</span>
                    <span className="truncate text-[13px] text-text-2">
                      {hyp.cause.replace(/_/g, " ")}
                    </span>
                    <span className="font-mono text-[9px] uppercase tracking-[0.07em] text-text-3">
                      {hyp.source}
                    </span>
                  </span>
                  <ConfidenceBar value={hyp.confidence} className="shrink-0" />
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {report.stripped_claims.length > 0 ? (
          <p className="mt-3 border-t border-accent-border pt-2 font-mono text-2xs text-text-3">
            {report.stripped_claims.length} unsupported claim
            {report.stripped_claims.length === 1 ? "" : "s"} stripped for lack of evidence.
          </p>
        ) : null}
      </Zone>

      {/* zone 3 — recommended action */}
      <Zone icon={ClipboardCheck} kicker="Recommended action · advisory, policy-gated" tone="plain">
        {actions.length === 0 ? (
          <p className="text-xs text-text-3">No action is recommended for this incident.</p>
        ) : (
          <div className="space-y-2.5">
            {actions.map((action, i) => (
              <ActionCard
                key={`${action.action_type}-${i}`}
                action={action}
                primary={i === 0 && report.recommended_next_step !== null}
              />
            ))}
          </div>
        )}
      </Zone>

      {/* uncertainties */}
      {report.uncertainties.length > 0 ? (
        <div className="rounded-md border border-dashed border-border-strong px-4 py-3">
          <h4 className="font-mono text-[10px] uppercase tracking-[0.11em] text-text-3">
            uncertainties
          </h4>
          <ul className="mt-2 space-y-1">
            {report.uncertainties.map((u, i) => (
              <li key={i} className="text-xs leading-relaxed text-text-2">
                · {u}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* provenance footer */}
      <p className="font-mono text-2xs tabular-nums text-text-3">
        report {shortRef(report.id)} · {formatDateTime(report.created_at)}
        {report.duration_ms !== null ? ` · ${formatNumber(report.duration_ms)} ms` : ""}
        {report.tokens_used !== null ? ` · ${formatNumber(report.tokens_used)} tokens` : ""}
        {report.tools_called.length > 0 ? ` · tools: ${report.tools_called.join(", ")}` : ""}
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* panel with fetch / trigger / polling                                */
/* ------------------------------------------------------------------ */

async function fetchReportOrNull(incidentId: string): Promise<InvestigationReport | null> {
  try {
    const raw = await api.incidents.investigation(incidentId);
    return parseInvestigationReport(raw);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

/**
 * AI investigation panel. Loads the latest report (404 → "not run yet"),
 * triggers POST /investigate, and polls GET /investigation while a run is
 * in flight. The report renders in three strictly separated zones —
 * OBSERVED FACTS (deterministic, slate) / AI INFERENCE (probabilistic,
 * amber) / RECOMMENDED ACTION (advisory, policy-gated) — with honesty
 * badges for heuristic/degraded/escalated states.
 */
export function InvestigationPanel({ incidentId }: { incidentId: string }) {
  const queryClient = useQueryClient();
  const [awaiting, setAwaiting] = React.useState(false);
  const awaitingSince = React.useRef<number>(0);
  const pollTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const queryKey = React.useMemo(() => ["incidents", "investigation", incidentId], [incidentId]);

  const query = useQuery({
    queryKey,
    queryFn: () => fetchReportOrNull(incidentId),
    retry: (failureCount, error) =>
      !(error instanceof ApiError && error.status === 404) && failureCount < 1,
    refetchInterval: awaiting ? POLL_INTERVAL_MS : false,
  });

  const stopAwaiting = React.useCallback(() => {
    setAwaiting(false);
    if (pollTimer.current !== null) {
      clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  const startAwaiting = React.useCallback(() => {
    awaitingSince.current = Date.now();
    setAwaiting(true);
    if (pollTimer.current !== null) clearTimeout(pollTimer.current);
    pollTimer.current = setTimeout(() => setAwaiting(false), POLL_TIMEOUT_MS);
  }, []);

  React.useEffect(() => () => stopAwaiting(), [stopAwaiting]);

  /* A report arriving (from any source) ends the wait. */
  React.useEffect(() => {
    if (query.data) stopAwaiting();
  }, [query.data, stopAwaiting]);

  const mutation = useMutation({
    mutationFn: async (forceRefresh: boolean) => {
      const raw = await api.incidents.investigate(incidentId, { force_refresh: forceRefresh });
      return parseInvestigateResponse(raw);
    },
    onSuccess: (parsed) => {
      if (parsed?.report) {
        queryClient.setQueryData(queryKey, parsed.report);
        stopAwaiting();
      } else if (parsed?.status === "running") {
        startAwaiting();
      } else {
        /* completed but unparseable body — refetch the canonical GET. */
        startAwaiting();
        void queryClient.invalidateQueries({ queryKey });
      }
    },
  });

  const running = mutation.isPending || awaiting;

  const actions = query.data ? (
    <Button
      variant="secondary"
      size="sm"
      disabled={running}
      onClick={() => mutation.mutate(true)}
    >
      {running ? (
        <Loader2 className="size-4 animate-spin" strokeWidth={1.5} aria-hidden />
      ) : (
        <RefreshCw className="size-4" strokeWidth={1.5} aria-hidden />
      )}
      Re-run investigation
    </Button>
  ) : undefined;

  return (
    <SectionCard
      title="AI investigation"
      description="Advisory report from the reasoner. Facts, inferences and recommended actions are kept strictly separate; execution stays behind the policy engine."
      actions={actions}
    >
      {query.isPending ? (
        <div className="space-y-3" aria-busy="true" aria-label="Loading investigation">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : query.isError ? (
        <ErrorPanel error={query.error} onRetry={() => query.refetch()} />
      ) : query.data === null ? (
        <div className="space-y-4">
          <EmptyState
            icon={ScanSearch}
            title="No investigation yet"
            description="Run the AI investigator to collect evidence, rank hypotheses and propose a policy-checked next step for this incident."
          />
          {mutation.isError ? (
            <ErrorPanel error={mutation.error} onRetry={() => mutation.mutate(false)} />
          ) : null}
          <div>
            <Button disabled={running} onClick={() => mutation.mutate(false)}>
              {running ? (
                <Loader2 className="size-4 animate-spin" strokeWidth={1.5} aria-hidden />
              ) : (
                <ScanSearch className="size-4" strokeWidth={1.5} aria-hidden />
              )}
              {running ? "Investigating…" : "Run AI investigation"}
            </Button>
            {awaiting ? (
              <p className="mt-2 font-mono text-2xs text-text-3" aria-live="polite">
                investigation in progress — polling for the report…
              </p>
            ) : null}
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          {mutation.isError ? (
            <ErrorPanel
              error={mutation.error}
              title="Re-run failed"
              onRetry={() => mutation.mutate(true)}
            />
          ) : null}
          {awaiting ? (
            <p className="font-mono text-2xs text-text-3" aria-live="polite">
              investigation in progress — polling for the report…
            </p>
          ) : null}
          <InvestigationReportView report={query.data} />
        </div>
      )}
    </SectionCard>
  );
}

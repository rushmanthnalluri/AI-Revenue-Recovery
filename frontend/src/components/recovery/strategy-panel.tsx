"use client";

import * as React from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ChevronDown, Play, ShieldCheck, X } from "lucide-react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, toApiError } from "@/lib/api";
import { formatINR } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ConfidenceBar } from "@/components/confidence-bar";
import { EmptyState } from "@/components/empty-state";
import { ErrorPanel } from "@/components/error-panel";
import { StatusPill } from "@/components/status-pill";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  actionTypeLabel,
  parseActionResponse,
  parsePlan,
  type ActionResponseView,
  type RecoveryPlanView,
  type StrategyOptionView,
} from "@/components/recovery/recovery-contract";
import { CONSOLE_ACTOR, useInvalidateRecovery, useModalA11y } from "@/components/recovery/recovery-hooks";

function usePlan(opportunityId: string) {
  return useQuery({
    queryKey: ["recovery", "plan", opportunityId],
    queryFn: () => api.recovery.plan(opportunityId).then(parsePlan),
  });
}

// ---------------------------------------------------------------------------
// execute confirmation dialog (floating layer — the one place shadows live)
// ---------------------------------------------------------------------------

interface ExecuteDialogProps {
  opportunityId: string;
  strategy: StrategyOptionView;
  amountPaise: number;
  onClose: () => void;
  onExecuted: (result: ActionResponseView) => void;
}

function ExecuteDialog({
  opportunityId,
  strategy,
  amountPaise,
  onClose,
  onExecuted,
}: ExecuteDialogProps) {
  const reduce = useReducedMotion();
  const invalidate = useInvalidateRecovery();
  const panelRef = React.useRef<HTMLDivElement>(null);
  useModalA11y(panelRef, onClose);

  const execute = useMutation({
    mutationFn: () =>
      api.recovery
        .execute(opportunityId, { strategy_id: strategy.id, actor: CONSOLE_ACTOR })
        .then(parseActionResponse),
    onSuccess: (result) => {
      invalidate();
      onExecuted(result);
    },
  });

  return (
    <motion.div
      className="fixed inset-0 z-[70] flex items-center justify-center p-4"
      initial={reduce ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.2 }}
    >
      <div
        aria-hidden
        className="absolute inset-0 bg-black/60"
        onClick={execute.isPending ? undefined : onClose}
      />
      <motion.div
        ref={panelRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="execute-dialog-title"
        aria-describedby="execute-dialog-desc"
        tabIndex={-1}
        className="relative w-full max-w-md rounded-lg border border-border-strong bg-elevated p-5 shadow-float"
        initial={reduce ? false : { opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: 8 }}
        transition={{ duration: 0.3, ease: [0.32, 0.72, 0, 1] }}
      >
        <p className="font-mono text-[10px] uppercase tracking-[0.11em] text-text-3">
          Confirm execution
        </p>
        <h3 id="execute-dialog-title" className="mt-1.5 text-base font-semibold text-text">
          {actionTypeLabel(strategy.action_type)}
        </h3>
        <div id="execute-dialog-desc" className="mt-2 space-y-2 text-[13px] text-text-2">
          <p>
            Execute this strategy for{" "}
            <span className="font-mono tabular-nums text-text">{formatINR(amountPaise)}</span>?
          </p>
          <p>
            The deterministic policy gate evaluates first: execution fires only on{" "}
            <span className="font-mono text-success">ALLOWED</span>;{" "}
            <span className="font-mono text-accent">REQUIRES_APPROVAL</span> routes the action to
            the Approval Center instead.
          </p>
        </div>

        {execute.isError ? (
          <div
            role="alert"
            className="mt-3 rounded-lg border border-[rgba(198,93,85,0.45)] bg-danger-dim px-3 py-2.5 text-[13px] text-text-2"
          >
            <span className="font-medium text-danger">
              Execution refused ({toApiError(execute.error).status || "network"})
            </span>{" "}
            — {toApiError(execute.error).message}
          </div>
        ) : null}

        <div className="mt-5 flex items-center justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={onClose} disabled={execute.isPending}>
            Cancel
          </Button>
          <Button size="sm" onClick={() => execute.mutate()} disabled={execute.isPending}>
            <Play aria-hidden />
            {execute.isPending ? "Executing…" : "Execute"}
          </Button>
        </div>
      </motion.div>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// "why chosen" expandable panel
// ---------------------------------------------------------------------------

function WhyChosenPanel({ plan }: { plan: RecoveryPlanView }) {
  const recommended =
    plan.strategies.find((s) => s.id === plan.recommended_strategy_id) ??
    plan.strategies.find((s) => s.selected) ??
    null;

  if (!recommended) return null;

  const constraintEntries = Object.entries(recommended.constraints ?? {});
  const preview = plan.policy_preview;

  return (
    <div className="space-y-3 rounded-b-lg border border-t-0 border-border bg-bg px-4 py-3.5">
      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.11em] text-text-3">
          Why {actionTypeLabel(recommended.action_type)} was chosen
        </p>
        <p className="mt-1 text-[13px] text-text-2">
          {recommended.reason ?? "Ranked highest by the strategy generator."}
        </p>
      </div>

      {constraintEntries.length > 0 ? (
        <div>
          <p className="font-mono text-[10px] uppercase tracking-[0.11em] text-text-3">
            Constraints
          </p>
          <dl className="mt-1.5 flex flex-wrap gap-1.5">
            {constraintEntries.map(([key, value]) => (
              <div
                key={key}
                className="rounded-sm border border-border-strong px-[7px] py-[3px] font-mono text-[10px] text-text-2"
              >
                <dt className="sr-only">{key}</dt>
                <dd>
                  {key}={" "}
                  <span className="text-text">
                    {typeof value === "number" ? value.toLocaleString("en-IN") : String(value)}
                  </span>
                </dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}

      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.11em] text-text-3">
          Policy preview — what the gate says right now
        </p>
        {preview ? (
          <div className="mt-1.5 space-y-1.5">
            <div className="flex items-center gap-2">
              <Badge
                variant={
                  preview.outcome === "ALLOWED"
                    ? "success"
                    : preview.outcome === "BLOCKED"
                      ? "danger"
                      : "warning"
                }
              >
                {preview.outcome.replace(/_/g, " ")}
              </Badge>
              <span className="text-2xs text-text-3">
                live evaluation through the deterministic gate
              </span>
            </div>
            {preview.reasons.length > 0 ? (
              <ul className="list-inside list-disc space-y-0.5 text-xs text-text-2">
                {preview.reasons.map((reason, i) => (
                  <li key={i}>{reason}</li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : (
          <p className="mt-1 text-xs text-text-3">
            No policy preview was returned for the recommended strategy.
          </p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// strategy comparison table
// ---------------------------------------------------------------------------

interface StrategyPanelProps {
  opportunityId: string;
  amountPaise: number;
}

/**
 * Strategy comparison for one opportunity (GET /recovery/{id}/plan): every
 * candidate strategy with expected recovery, risk, confidence and policy
 * eligibility; the recommended row carries the amber inset, and its rationale
 * + live policy preview expand below the table.
 */
export function StrategyPanel({ opportunityId, amountPaise }: StrategyPanelProps) {
  const plan = usePlan(opportunityId);
  const [whyOpen, setWhyOpen] = React.useState(false);
  const [executeTarget, setExecuteTarget] = React.useState<StrategyOptionView | null>(null);
  const [lastResult, setLastResult] = React.useState<ActionResponseView | null>(null);

  if (plan.isPending) {
    return (
      <div className="space-y-2" aria-busy="true" aria-label="Loading strategy comparison">
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
      </div>
    );
  }

  if (plan.isError) {
    return <ErrorPanel error={plan.error} onRetry={() => plan.refetch()} />;
  }

  const data = plan.data;
  const strategies = [...data.strategies].sort((a, b) => a.rank - b.rank);

  if (strategies.length === 0) {
    return (
      <EmptyState
        title="No strategies generated"
        description="The strategy generator has not produced candidates for this opportunity yet."
      />
    );
  }

  const hasRecommendation = strategies.some(
    (s) => s.id === data.recommended_strategy_id || s.selected,
  );

  return (
    <div>
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead className="w-8">#</TableHead>
            <TableHead>Strategy</TableHead>
            <TableHead className="text-right">Expected recovery</TableHead>
            <TableHead>Risk</TableHead>
            <TableHead>Confidence</TableHead>
            <TableHead>Eligibility</TableHead>
            <TableHead className="text-right">Action</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {strategies.map((strategy) => {
            const recommended =
              strategy.id === data.recommended_strategy_id || (!hasRecommendation && strategy.selected);
            return (
              <TableRow
                key={strategy.id}
                className={cn(
                  recommended && "bg-accent-wash hover:bg-accent-wash",
                  !strategy.eligibility && "opacity-60",
                )}
              >
                <TableCell
                  className={cn(
                    "font-mono text-xs tabular-nums text-text-3",
                    recommended && "border-l-2 border-l-accent",
                  )}
                >
                  {strategy.rank}
                </TableCell>
                <TableCell>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[13px] font-medium text-text">
                      {actionTypeLabel(strategy.action_type)}
                    </span>
                    {recommended ? <Badge variant="accent">Recommended</Badge> : null}
                    <span className="font-mono text-2xs text-text-3">{strategy.generated_by}</span>
                  </div>
                </TableCell>
                <TableCell className="text-right font-mono text-xs tabular-nums text-text">
                  {formatINR(strategy.expected_recovery_paise)}
                </TableCell>
                <TableCell className="text-xs capitalize text-text-2">{strategy.risk}</TableCell>
                <TableCell>
                  <ConfidenceBar value={strategy.confidence} />
                </TableCell>
                <TableCell>
                  {strategy.eligibility ? (
                    <Badge variant="success">Eligible</Badge>
                  ) : (
                    <Badge variant="danger">Ineligible</Badge>
                  )}
                </TableCell>
                <TableCell className="text-right">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!strategy.eligibility}
                    title={
                      strategy.eligibility
                        ? `Execute ${actionTypeLabel(strategy.action_type)}`
                        : "The strategy generator marked this option ineligible for the opportunity"
                    }
                    onClick={() => {
                      setLastResult(null);
                      setExecuteTarget(strategy);
                    }}
                  >
                    Execute
                  </Button>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>

      {hasRecommendation ? (
        <>
          <button
            type="button"
            aria-expanded={whyOpen}
            aria-controls="why-chosen-panel"
            onClick={() => setWhyOpen((v) => !v)}
            className="flex w-full items-center justify-between rounded-b-lg border border-t-0 border-border bg-surface px-4 py-2 text-left font-mono text-[10px] uppercase tracking-[0.11em] text-text-3 transition-colors duration-150 ease-apple hover:text-text-2 aria-[expanded=true]:rounded-b-none"
          >
            <span className="flex items-center gap-1.5">
              <ShieldCheck className="size-3.5 text-accent" strokeWidth={1.5} aria-hidden />
              Why this strategy — rationale, constraints, policy preview
            </span>
            <ChevronDown
              className={cn("size-3.5 transition-transform duration-150", whyOpen && "rotate-180")}
              strokeWidth={1.5}
              aria-hidden
            />
          </button>
          {whyOpen ? (
            <div id="why-chosen-panel">
              <WhyChosenPanel plan={data} />
            </div>
          ) : null}
        </>
      ) : null}

      {lastResult ? (
        <div className="mt-3 rounded-lg border border-accent-border bg-accent-wash px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <StatusPill status={lastResult.status} />
              <p className="text-[13px] text-text-2">{lastResult.message}</p>
            </div>
            <button
              type="button"
              aria-label="Dismiss execution result"
              onClick={() => setLastResult(null)}
              className="text-text-3 transition-colors hover:text-text"
            >
              <X className="size-4" strokeWidth={1.5} aria-hidden />
            </button>
          </div>
          {lastResult.policy_decision ? (
            <p className="mt-1 font-mono text-2xs text-text-3">
              gate: {lastResult.policy_decision.outcome}
              {lastResult.policy_decision.rules_matched.length > 0
                ? ` · rules: ${lastResult.policy_decision.rules_matched.join(", ")}`
                : ""}
            </p>
          ) : null}
        </div>
      ) : null}

      <AnimatePresence>
        {executeTarget ? (
          <ExecuteDialog
            key={executeTarget.id}
            opportunityId={opportunityId}
            strategy={executeTarget}
            amountPaise={amountPaise}
            onClose={() => setExecuteTarget(null)}
            onExecuted={(result) => {
              setExecuteTarget(null);
              setLastResult(result);
            }}
          />
        ) : null}
      </AnimatePresence>
    </div>
  );
}

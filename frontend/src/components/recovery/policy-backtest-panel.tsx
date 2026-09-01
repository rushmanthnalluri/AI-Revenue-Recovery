"use client";

import * as React from "react";
import { FlaskConical, Loader2 } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type {
  Environment,
  PolicyBacktestFlip,
  PolicyBacktestRequest,
  PolicyOutcome,
  PolicyTransitionImpact,
} from "@/lib/types";
import { formatDateTime, formatINR, formatNumber, timeAgo } from "@/lib/format";
import { DataTable, type ColumnDef } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorPanel } from "@/components/error-panel";
import { MetricStrip } from "@/components/metric-strip";
import { SectionCard } from "@/components/section-card";
import { StatusPill } from "@/components/status-pill";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { actionTypeLabel } from "@/components/recovery/recovery-contract";

/** Flips table display cap — the response carries every flip; the table shows
    the first page-worth with an honest truncation note. */
const MAX_FLIP_ROWS = 25;
const MAX_RULE_ROWS = 8;

const OUTCOMES: PolicyOutcome[] = ["ALLOWED", "BLOCKED", "REQUIRES_APPROVAL"];

const WINDOWS = [
  { id: "all", label: "All time", ms: null },
  { id: "24h", label: "Last 24h", ms: 24 * 60 * 60 * 1000 },
  { id: "7d", label: "Last 7 days", ms: 7 * 24 * 60 * 60 * 1000 },
  { id: "30d", label: "Last 30 days", ms: 30 * 24 * 60 * 60 * 1000 },
] as const;
type WindowId = (typeof WINDOWS)[number]["id"];

const KICKER = "font-mono text-[11px] uppercase tracking-[0.11em] text-text-3";

interface TallyRow {
  outcome: PolicyOutcome;
  original: number;
  replayed: number;
}

const tallyColumns: ColumnDef<TallyRow>[] = [
  {
    key: "outcome",
    header: "Outcome",
    render: (row) => <StatusPill status={row.outcome} />,
  },
  {
    key: "original",
    header: "As recorded",
    className: "text-right",
    render: (row) => <span className="tnum text-sm">{formatNumber(row.original)}</span>,
  },
  {
    key: "replayed",
    header: "Under current policy",
    className: "text-right",
    render: (row) => <span className="tnum text-sm">{formatNumber(row.replayed)}</span>,
  },
];

const transitionColumns: ColumnDef<PolicyTransitionImpact>[] = [
  {
    key: "transition",
    header: "Transition",
    render: (row) => (
      <span className="flex flex-wrap items-center gap-1.5">
        <StatusPill status={row.from_outcome} />
        <span aria-hidden className="text-text-3">→</span>
        <StatusPill status={row.to_outcome} />
      </span>
    ),
  },
  {
    key: "count",
    header: "Decisions",
    className: "text-right",
    render: (row) => <span className="tnum text-sm">{formatNumber(row.count)}</span>,
  },
  {
    key: "amount",
    header: "Amount impact",
    className: "text-right",
    render: (row) => <span className="tnum text-sm">{formatINR(row.amount_paise)}</span>,
  },
];

const flipColumns: ColumnDef<PolicyBacktestFlip>[] = [
  {
    key: "decision",
    header: "Decision",
    render: (row) => (
      <div className="max-w-[180px]">
        <p className="truncate font-mono text-xs text-text-2" title={row.decision_id}>
          {row.decision_id}
        </p>
        {row.action_id ? (
          <p className="truncate font-mono text-2xs text-text-3" title={row.action_id}>
            {row.action_id}
          </p>
        ) : null}
      </div>
    ),
  },
  {
    key: "action",
    header: "Action",
    render: (row) => <span className="text-xs text-text-2">{actionTypeLabel(row.action_type)}</span>,
  },
  {
    key: "amount",
    header: "Amount",
    className: "text-right",
    render: (row) => <span className="tnum text-sm">{formatINR(row.amount_paise)}</span>,
  },
  {
    key: "actor",
    header: "Actor",
    render: (row) => <span className="font-mono text-xs text-text-3">{row.actor}</span>,
  },
  {
    key: "decided",
    header: "Decided",
    render: (row) => (
      <span className="text-xs tnum text-text-3" title={formatDateTime(row.decided_at)}>
        {timeAgo(row.decided_at)}
      </span>
    ),
  },
  {
    key: "flip",
    header: "Flip",
    render: (row) => (
      <span className="flex flex-wrap items-center gap-1.5">
        <StatusPill status={row.original_outcome} />
        <span aria-hidden className="text-text-3">→</span>
        <StatusPill status={row.replayed_outcome} />
      </span>
    ),
  },
  {
    key: "rules",
    header: "Rules now matched",
    render: (row) =>
      row.replayed_rules.length === 0 ? (
        <span className="text-xs text-text-3">—</span>
      ) : (
        <span
          className="flex max-w-[220px] flex-wrap gap-1"
          title={
            row.original_rules.length > 0
              ? `originally matched: ${row.original_rules.join(", ")}`
              : undefined
          }
        >
          {row.replayed_rules.slice(0, 3).map((rule) => (
            <span
              key={rule}
              className="rounded-sm border border-border-strong px-[7px] py-[3px] font-mono text-[10px] text-text-2"
            >
              {rule}
            </span>
          ))}
          {row.replayed_rules.length > 3 ? (
            <span className="font-mono text-[10px] text-text-3">
              +{row.replayed_rules.length - 3}
            </span>
          ) : null}
        </span>
      ),
  },
];

interface RuleRow {
  rule: string;
  replayed: number;
  original: number;
}

const ruleColumns: ColumnDef<RuleRow>[] = [
  {
    key: "rule",
    header: "Rule",
    render: (row) => <span className="font-mono text-xs text-text-2">{row.rule}</span>,
  },
  {
    key: "replayed",
    header: "Hits under current policy",
    className: "text-right",
    render: (row) => <span className="tnum text-sm">{formatNumber(row.replayed)}</span>,
  },
  {
    key: "original",
    header: "As originally recorded",
    className: "text-right",
    render: (row) => <span className="tnum text-sm text-text-3">{formatNumber(row.original)}</span>,
  },
];

/**
 * Policy backtest (POST /api/v1/policy/backtest) — replays stored policy
 * decisions against the CURRENT policy document and reports outcome tallies,
 * flips, per-transition paise impact and per-rule hits. The replay itself is
 * read-only, but the run joins the audit trail (same convention as
 * detection.run / reconciliation), so it sits behind the house two-step
 * confirm like the reconciliation sweep beside it. Sits directly under the
 * Recovery pipeline card, next to where the operator already runs the sweep.
 */
export function PolicyBacktestPanel() {
  const queryClient = useQueryClient();
  const [environment, setEnvironment] = React.useState<"all" | Environment>("all");
  const [windowId, setWindowId] = React.useState<WindowId>("all");
  const [confirming, setConfirming] = React.useState(false);

  const backtest = useMutation({
    mutationFn: (body: PolicyBacktestRequest) => api.policy.backtest(body),
    onSuccess: () => {
      setConfirming(false);
      // The run records its own `policy.backtest` audit row.
      void queryClient.invalidateQueries({ queryKey: ["audit"] });
    },
  });
  const report = backtest.data ?? null;

  const run = () => {
    const body: PolicyBacktestRequest = {};
    if (environment !== "all") body.environment = environment;
    const win = WINDOWS.find((w) => w.id === windowId);
    if (win?.ms) body.since = new Date(Date.now() - win.ms).toISOString();
    backtest.mutate(body);
  };

  const tallies: TallyRow[] = report
    ? OUTCOMES.map((outcome) => ({
        outcome,
        original: report.outcomes_original[outcome] ?? 0,
        replayed: report.outcomes_replayed[outcome] ?? 0,
      }))
    : [];

  const topRules: RuleRow[] = report
    ? [...new Set([...Object.keys(report.rule_hits), ...Object.keys(report.rule_hits_original)])]
        .map((rule) => ({
          rule,
          replayed: report.rule_hits[rule] ?? 0,
          original: report.rule_hits_original[rule] ?? 0,
        }))
        .sort((a, b) => b.replayed - a.replayed || b.original - a.original)
        .slice(0, MAX_RULE_ROWS)
    : [];

  const flipsShown = report ? report.flips.slice(0, MAX_FLIP_ROWS) : [];

  return (
    <SectionCard
      title="Policy backtest"
      description="Replay stored policy decisions against the current policy file — which verdicts would stand, which would flip, and the paise impact. Read-only report; only the run itself joins the audit trail."
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Select
            aria-label="Backtest environment scope"
            value={environment}
            onChange={(e) => setEnvironment(e.target.value as "all" | Environment)}
            disabled={backtest.isPending}
            className="h-8 w-44 text-xs"
          >
            <option value="all">All environments</option>
            <option value="real_test">Real Test (Razorpay)</option>
            <option value="research">Research (synthetic)</option>
          </Select>
          <Select
            aria-label="Backtest decision window"
            value={windowId}
            onChange={(e) => setWindowId(e.target.value as WindowId)}
            disabled={backtest.isPending}
            className="h-8 w-32 text-xs"
          >
            {WINDOWS.map((w) => (
              <option key={w.id} value={w.id}>
                {w.label}
              </option>
            ))}
          </Select>
          {confirming ? (
            <>
              <Button size="sm" disabled={backtest.isPending} onClick={run}>
                {backtest.isPending ? (
                  <Loader2 className="size-4 animate-spin" strokeWidth={1.5} aria-hidden />
                ) : (
                  <FlaskConical className="size-4" strokeWidth={1.5} aria-hidden />
                )}
                {backtest.isPending ? "Running…" : "Confirm backtest"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={backtest.isPending}
                onClick={() => setConfirming(false)}
              >
                Cancel
              </Button>
            </>
          ) : (
            <Button
              variant="outline"
              size="sm"
              title="Replay stored policy decisions against the current policy file (read-only; the run itself is audited)"
              onClick={() => setConfirming(true)}
            >
              <FlaskConical className="size-4" strokeWidth={1.5} aria-hidden />
              Run policy backtest
            </Button>
          )}
        </div>
      }
    >
      {backtest.isError ? (
        <ErrorPanel
          error={backtest.error}
          onRetry={run}
          title="Backtest failed"
        />
      ) : backtest.isPending ? (
        <div className="space-y-2" aria-busy="true" aria-label="Running policy backtest">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : !report ? (
        <EmptyState
          icon={FlaskConical}
          title="No backtest run yet"
          description="Replays every stored policy decision (optionally scoped by environment and window) against the current policy file and reports what would change. Nothing is executed or written except the audit row recording the run."
        />
      ) : (
        <div className="space-y-5">
          <dl className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
            <div className="flex items-center gap-1.5">
              <dt>run</dt>
              <dd className="normal-case text-text-2">{report.run_id}</dd>
            </div>
            <div className="flex items-center gap-1.5">
              <dt>policy</dt>
              <dd className="normal-case text-text-2">v{report.policy_version}</dd>
            </div>
            <div className="flex items-center gap-1.5">
              <dt>env</dt>
              <dd className="normal-case text-text-2">{report.environment ?? "all"}</dd>
            </div>
            <div className="flex items-center gap-1.5">
              <dt>window</dt>
              <dd className="normal-case text-text-2">
                {report.since
                  ? `${formatDateTime(report.since)} → ${report.until ? formatDateTime(report.until) : "now"}`
                  : "all time"}
              </dd>
            </div>
            {report.finished_at ? (
              <div className="flex items-center gap-1.5">
                <dt>finished</dt>
                <dd className="normal-case text-text-2">{timeAgo(report.finished_at)}</dd>
              </div>
            ) : null}
          </dl>

          <MetricStrip
            items={[
              {
                key: "scanned",
                label: "Decisions replayed",
                value: formatNumber(report.decisions_scanned),
                hint: "stored policy decisions in scope",
              },
              {
                key: "unchanged",
                label: "Unchanged",
                value: formatNumber(report.unchanged_count),
                tone: "success",
                hint: "verdict stands under current policy",
              },
              {
                key: "flips",
                label: "Flips",
                value: formatNumber(report.flip_count),
                tone: report.flip_count > 0 ? "warning" : "default",
                hint: "outcome would change under current policy",
              },
            ]}
          />

          {report.decisions_scanned === 0 ? (
            <p className="rounded-md border border-border bg-raised/40 px-3 py-2 text-xs text-text-2">
              No stored policy decisions in this scope — decisions are recorded as the
              policy gate evaluates recovery actions.
              {report.detail ? <span className="mt-1 block text-text-3">{report.detail}</span> : null}
            </p>
          ) : (
            <>
              <section className="space-y-2">
                <p className={KICKER}>Outcome tallies</p>
                <DataTable
                  columns={tallyColumns}
                  rows={tallies}
                  getRowId={(r) => r.outcome}
                />
              </section>

              {report.transitions.length > 0 ? (
                <section className="space-y-2">
                  <p className={KICKER}>Per-transition paise impact</p>
                  <DataTable
                    columns={transitionColumns}
                    rows={report.transitions}
                    getRowId={(r) => `${r.from_outcome}-${r.to_outcome}`}
                  />
                </section>
              ) : null}

              {report.flips.length > 0 ? (
                <section className="space-y-2">
                  <p className={KICKER}>Flips</p>
                  <DataTable
                    columns={flipColumns}
                    rows={flipsShown}
                    getRowId={(r) => r.decision_id}
                  />
                  {report.flips.length > flipsShown.length ? (
                    <p className="font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
                      first <span className="tnum normal-case text-text-2">{formatNumber(flipsShown.length)}</span>
                      {" of "}
                      <span className="tnum normal-case text-text-2">{formatNumber(report.flips.length)}</span>
                      {" flips shown"}
                    </p>
                  ) : null}
                </section>
              ) : null}

              {topRules.length > 0 ? (
                <section className="space-y-2">
                  <p className={KICKER}>Top rules</p>
                  <DataTable
                    columns={ruleColumns}
                    rows={topRules}
                    getRowId={(r) => r.rule}
                  />
                </section>
              ) : null}

              {report.detail ? (
                <p className="font-mono text-2xs text-text-3">{report.detail}</p>
              ) : null}
            </>
          )}
        </div>
      )}
    </SectionCard>
  );
}

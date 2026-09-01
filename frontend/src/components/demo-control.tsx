"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Play, RotateCcw, TriangleAlert } from "lucide-react";

import { ApiError, api } from "@/lib/api";
import type { DemoResetResponse, ScenarioTriggerResponse } from "@/lib/types";
import { DemoResetSummary, DemoRunSummary } from "@/components/demo-run-summary";
import { EmptyState } from "@/components/empty-state";
import { ErrorPanel } from "@/components/error-panel";
import { SectionCard } from "@/components/section-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

/** True when the request gave up waiting but the backend may still be working. */
function isStillRunning(error: unknown): boolean {
  return error instanceof ApiError && error.isUnreachable;
}

/**
 * Scenario runner — how the Research Lab dataset is driven. Lists the
 * simulator scenarios (GET /demo/scenarios), triggers them
 * (POST /demo/scenario/{name} — an idempotent seed + one anchored detection
 * pass), and resets the research dataset (POST /demo/reset, behind an
 * explicit two-step confirm). Every rendered number comes from the real API
 * responses. All of it is synthetic research data, pinned server-side to the
 * research environment — the real merchant environment is never touched.
 *
 * Note: the typed client gives scenario triggers 120s (LONG_RUNNING_TIMEOUT_MS);
 * a large seed can still outlive it. A timeout is shown as "still running
 * server-side" rather than a failure — the 15s dashboard poll surfaces the
 * data as it lands.
 */
export function DemoControl() {
  const queryClient = useQueryClient();
  const [lastRun, setLastRun] = React.useState<ScenarioTriggerResponse | null>(null);
  const [lastReset, setLastReset] = React.useState<DemoResetResponse | null>(null);
  const [confirmReset, setConfirmReset] = React.useState(false);

  const scenarios = useQuery({
    queryKey: ["demo", "scenarios"],
    queryFn: () => api.demo.scenarios(),
    staleTime: 5 * 60_000,
  });

  const refreshDashboard = React.useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    void queryClient.invalidateQueries({ queryKey: ["incidents"] });
    void queryClient.invalidateQueries({ queryKey: ["recovery"] });
    void queryClient.invalidateQueries({ queryKey: ["audit"] });
  }, [queryClient]);

  const trigger = useMutation({
    mutationFn: (name: string) => api.demo.triggerScenario(name),
    onSuccess: (data) => {
      setLastRun(data);
      setLastReset(null);
      refreshDashboard();
    },
    onError: (error) => {
      // A timeout/unreachable means the seed may still be running server-side.
      if (isStillRunning(error)) refreshDashboard();
    },
  });

  const reset = useMutation({
    mutationFn: () => api.demo.reset(),
    onSuccess: (data) => {
      setLastReset(data);
      setLastRun(null);
      setConfirmReset(false);
      // Everything derived was just deleted — refetch every surface.
      void queryClient.invalidateQueries();
    },
  });

  const busy = trigger.isPending || reset.isPending;

  return (
    <div id="scenario-runner" className="scroll-mt-20">
    <SectionCard
      title="Scenario runner"
      description="Deterministic simulator scenarios — seed the synthetic research dataset, run one anchored detection pass, and watch detection, diagnosis and recovery react. Research data only; the real merchant environment is never touched."
      actions={
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => setConfirmReset(true)}
        >
          <RotateCcw aria-hidden />
          Reset research data
        </Button>
      }
      contentClassName="space-y-4"
    >
      {/* Scenario triggers */}
      {scenarios.isPending ? (
        <div aria-busy="true" aria-label="Loading scenarios" className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      ) : scenarios.isError ? (
        <ErrorPanel
          error={scenarios.error}
          onRetry={() => scenarios.refetch()}
          title="Scenario list unavailable"
        />
      ) : scenarios.data.scenarios.length === 0 ? (
        <EmptyState
          title="No scenarios advertised"
          description="The backend returned an empty scenario list. Check that the simulator presets are registered."
        />
      ) : (
        <ul className="divide-y divide-border" aria-label="Research scenarios">
          {scenarios.data.scenarios.map((scenario) => {
            const runningThis = trigger.isPending && trigger.variables === scenario.name;
            return (
              <li key={scenario.name} className="flex flex-wrap items-center gap-3 py-3">
                <div className="min-w-0 flex-1">
                  <p className="font-mono text-[13px] text-text">{scenario.name}</p>
                  <p className="mt-0.5 text-xs text-text-3">{scenario.description}</p>
                </div>
                {scenario.expected_incident_metric ? (
                  <Badge variant="outline" title="Metric the injected incident degrades">
                    {scenario.expected_incident_metric}
                  </Badge>
                ) : null}
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={busy}
                  aria-label={`Run scenario ${scenario.name}`}
                  onClick={() => trigger.mutate(scenario.name)}
                >
                  {runningThis ? (
                    <Loader2 aria-hidden className="animate-spin" />
                  ) : (
                    <Play aria-hidden />
                  )}
                  {runningThis ? "Running" : "Run"}
                </Button>
              </li>
            );
          })}
        </ul>
      )}

      {trigger.isPending ? (
        <p className="flex items-center gap-2 text-xs text-text-3" role="status">
          <Loader2 aria-hidden className="size-3.5 animate-spin" />
          Seeding {trigger.variables} and running one anchored detection pass — large
          scenarios can take up to a minute.
        </p>
      ) : null}

      {/* Trigger outcome — real run summary, or an honest status on timeout */}
      {trigger.isError ? (
        isStillRunning(trigger.error) ? (
          <div
            role="status"
            className="flex items-start gap-3 rounded-lg border border-accent-border bg-accent-wash px-4 py-3.5"
          >
            <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-accent" />
            <div className="text-[13px] text-text-2">
              <p className="font-medium text-text">No response within 120 seconds</p>
              <p className="mt-0.5">
                The simulator run continues on the server. The dashboard polls every 15s
                and will surface the new data as it lands — no need to retrigger (runs are
                idempotent).
              </p>
            </div>
          </div>
        ) : (
          <ErrorPanel
            error={trigger.error}
            onRetry={() => {
              if (trigger.variables) trigger.mutate(trigger.variables);
            }}
            title="Scenario trigger failed"
          />
        )
      ) : null}

      {lastRun && !trigger.isPending ? <DemoRunSummary run={lastRun} /> : null}

      {/* Reset — two-step confirm; the destructive call only fires on confirm */}
      {confirmReset ? (
        <div
          role="alert"
          className="rounded-lg border border-[rgba(198,93,85,0.45)] bg-danger-dim px-4 py-3.5"
        >
          <p className="flex items-center gap-2 text-[13.5px] font-semibold text-danger">
            <TriangleAlert aria-hidden className="size-4" />
            Reset the research dataset?
          </p>
          <p className="mt-1 text-[13px] text-text-2">
            Deletes every simulator-seeded row and all derived research incidents, diagnoses,
            opportunities and recovery actions. Evaluation runs, model predictions and the
            audit trail are kept. Real merchant (Razorpay Test Mode) data is never touched.
          </p>
          <div className="mt-3 flex items-center gap-2">
            <Button
              variant="destructive"
              size="sm"
              disabled={reset.isPending}
              onClick={() => reset.mutate()}
            >
              {reset.isPending ? <Loader2 aria-hidden className="animate-spin" /> : null}
              {reset.isPending ? "Resetting" : "Confirm reset"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={reset.isPending}
              onClick={() => setConfirmReset(false)}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : null}

      {reset.isError ? (
        <ErrorPanel
          error={reset.error}
          onRetry={() => reset.mutate()}
          title="Reset failed"
        />
      ) : null}

      {lastReset && !reset.isPending ? <DemoResetSummary result={lastReset} /> : null}
    </SectionCard>
    </div>
  );
}

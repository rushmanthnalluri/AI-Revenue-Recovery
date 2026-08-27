"use client";

import * as React from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { api, ApiError } from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { PageHeader } from "@/components/page-header";
import { SectionCard } from "@/components/section-card";
import { EvaluationRunDetailView } from "@/components/evaluation/evaluation-run-detail";
import { EvaluationRunsTable } from "@/components/evaluation/evaluation-runs-table";
import { EvaluationRunTrigger } from "@/components/evaluation/evaluation-run-trigger";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { formatNumber } from "@/lib/format";

const POLL_MS = 4_000;

function runName(): string {
  // Distinct, sortable, human-readable: console-2026-08-27-00-07
  return `console-${new Date().toISOString().slice(0, 16).replace("T", "-").replace(":", "-")}`;
}

export function EvaluationView() {
  const queryClient = useQueryClient();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const selectedId = searchParams.get("run");
  const selectRun = React.useCallback(
    (id: string | null) => {
      router.replace(id ? `${pathname}?run=${encodeURIComponent(id)}` : pathname, {
        scroll: false,
      });
    },
    [router, pathname],
  );

  const scenarios = useQuery({
    queryKey: ["demo", "scenarios"],
    queryFn: () => api.demo.scenarios(),
  });

  const runs = useQuery({
    queryKey: ["evaluation", "runs"],
    queryFn: () => api.evaluation.runs({ page: 1, page_size: 20 }),
    placeholderData: keepPreviousData,
    // Poll while any stored run is still executing (POSTs are synchronous and
    // regularly outlive the client timeout — the stored row is the truth).
    refetchInterval: (query) =>
      query.state.data?.items.some((r) => r.status === "running") ? POLL_MS : false,
  });

  const detail = useQuery({
    queryKey: ["evaluation", "runs", selectedId],
    queryFn: () => api.evaluation.getRun(selectedId!),
    enabled: Boolean(selectedId),
    refetchInterval: (query) =>
      query.state.data?.status === "running" ? POLL_MS : false,
  });

  const [awaitingName, setAwaitingName] = React.useState<string | null>(null);

  const runMutation = useMutation({
    mutationFn: ({ name, scenario }: { name: string; scenario: string }) =>
      api.evaluation.run({ name, evaluation_type: "end_to_end", scenario }),
    onSuccess: (res) => {
      setAwaitingName(null);
      void queryClient.invalidateQueries({ queryKey: ["evaluation"] });
      selectRun(res.run_id);
    },
    onError: (error, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["evaluation"] });
      // The harness runs synchronously and usually outlives the 10s client
      // timeout — that is not a failure: the run row already exists and the
      // runs table polls until it completes.
      if (error instanceof ApiError && error.isUnreachable && error.code === "timeout") {
        setAwaitingName(variables.name);
      }
    },
  });

  const startRun = (scenario: string) => {
    runMutation.mutate({ name: runName(), scenario });
  };

  // When the awaited run row appears in the list, select it and stop awaiting.
  React.useEffect(() => {
    if (!awaitingName || !runs.data) return;
    const found = runs.data.items.find((r) => r.name === awaitingName);
    if (found) {
      setAwaitingName(null);
      selectRun(found.id);
    }
  }, [awaitingName, runs.data, selectRun]);

  // Default selection: newest completed run, else newest run.
  React.useEffect(() => {
    if (selectedId || !runs.data || runs.data.items.length === 0) return;
    const items = runs.data.items;
    const preferred = items.find((r) => r.status === "completed") ?? items[0]!;
    selectRun(preferred.id);
  }, [selectedId, runs.data, selectRun]);

  const timedOut =
    runMutation.error instanceof ApiError &&
    runMutation.error.isUnreachable &&
    runMutation.error.code === "timeout";
  const showMutationError = runMutation.isError && !timedOut;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Evaluation Lab"
        description="Detection, diagnosis, and recovery scored against simulator ground truth — two arms, same seed, stored rows only."
        actions={
          <EvaluationRunTrigger
            scenarios={scenarios.data?.scenarios}
            isPending={runMutation.isPending}
            onRun={startRun}
          />
        }
      />

      {awaitingName ? (
        <div className="flex items-center gap-2.5 rounded-lg border border-accent-border bg-accent-wash px-4 py-3.5 text-[13.5px] text-text-2">
          <Loader2 className="size-4 animate-spin text-accent" strokeWidth={1.5} aria-hidden />
          The harness executes synchronously and outlived the client timeout — the run is
          continuing on the server. This page polls the stored row and opens it as soon as it
          appears.
        </div>
      ) : null}

      {showMutationError ? (
        <ErrorPanel
          error={runMutation.error}
          title="Could not start the evaluation run"
          onRetry={() => runMutation.reset()}
        />
      ) : null}

      <SectionCard
        title="Evaluation runs"
        description="Newest first — select a row for the full stored payload"
        contentClassName="pt-0"
      >
        {runs.isError ? (
          <ErrorPanel error={runs.error} onRetry={() => runs.refetch()} />
        ) : (
          <>
            <EvaluationRunsTable
              runs={runs.data?.items}
              isLoading={runs.isPending}
              selectedId={selectedId}
              onSelect={(id) => selectRun(id)}
            />
            {runs.data && runs.data.total > runs.data.items.length ? (
              <p className="mt-3 font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
                {formatNumber(runs.data.total)} runs stored · showing latest{" "}
                {runs.data.items.length}
              </p>
            ) : null}
          </>
        )}
      </SectionCard>

      {selectedId ? (
        detail.isPending ? (
          <div className="space-y-4" aria-busy="true" aria-label="Loading run detail">
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-56 w-full" />
          </div>
        ) : detail.isError ? (
          <div className="space-y-3">
            <ErrorPanel
              error={detail.error}
              title="Could not load the selected run"
              onRetry={() => detail.refetch()}
            />
            <Button variant="secondary" size="sm" onClick={() => selectRun(null)}>
              Clear selection
            </Button>
          </div>
        ) : detail.data ? (
          <EvaluationRunDetailView run={detail.data} />
        ) : null
      ) : null}
    </div>
  );
}

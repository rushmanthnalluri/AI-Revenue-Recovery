"use client";

import * as React from "react";
import { FlaskConical } from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { StatusPill } from "@/components/status-pill";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatDateTime, formatMinutes } from "@/lib/format";
import type { EvaluationRunSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

interface EvaluationRunsTableProps {
  runs: EvaluationRunSummary[] | undefined;
  isLoading: boolean;
  selectedId: string | null;
  onSelect: (runId: string) => void;
}

function durationLabel(run: EvaluationRunSummary): string {
  if (!run.started_at) return "—";
  if (!run.finished_at) return run.status === "running" ? "running…" : "—";
  const ms = new Date(run.finished_at).getTime() - new Date(run.started_at).getTime();
  if (Number.isNaN(ms) || ms < 0) return "—";
  return formatMinutes(ms / 60_000);
}

/** Stored evaluation runs — click/keyboard selects a run for full detail. */
export function EvaluationRunsTable({
  runs,
  isLoading,
  selectedId,
  onSelect,
}: EvaluationRunsTableProps) {
  if (isLoading) {
    return (
      <div className="space-y-2" aria-busy="true" aria-label="Loading evaluation runs">
        <Skeleton className="h-9 w-full" />
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-11 w-full" />
        ))}
      </div>
    );
  }

  if (!runs || runs.length === 0) {
    return (
      <EmptyState
        icon={FlaskConical}
        title="No evaluation runs yet"
        description="Trigger a run to execute the two-arm harness (naive retry vs the full PulseRecover loop) against simulator ground truth."
      />
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead>Run</TableHead>
          <TableHead>Scenario</TableHead>
          <TableHead>Type</TableHead>
          <TableHead>Status</TableHead>
          <TableHead className="text-right">Started</TableHead>
          <TableHead className="text-right">Duration</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {runs.map((run) => {
          const selected = run.id === selectedId;
          return (
            <TableRow
              key={run.id}
              tabIndex={0}
              role="link"
              aria-current={selected ? "true" : undefined}
              data-state={selected ? "selected" : undefined}
              className={cn("cursor-pointer", selected && "bg-raised")}
              onClick={() => onSelect(run.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelect(run.id);
                }
              }}
            >
              <TableCell>
                <p className="text-sm font-medium text-text">{run.name}</p>
                <p className="font-mono text-2xs tabular-nums text-text-3">{run.id}</p>
              </TableCell>
              <TableCell>
                <span className="font-mono text-xs text-text-2">{run.dataset}</span>
              </TableCell>
              <TableCell>
                <span className="font-mono text-xs text-text-2">
                  {run.evaluation_type.replace(/_/g, " ")}
                </span>
              </TableCell>
              <TableCell>
                <StatusPill status={run.status} pulse={run.status === "running"} />
              </TableCell>
              <TableCell className="text-right">
                <span className="font-mono text-xs tabular-nums text-text-3">
                  {formatDateTime(run.started_at)}
                </span>
              </TableCell>
              <TableCell className="text-right">
                <span className="font-mono text-xs tabular-nums text-text-3">
                  {durationLabel(run)}
                </span>
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

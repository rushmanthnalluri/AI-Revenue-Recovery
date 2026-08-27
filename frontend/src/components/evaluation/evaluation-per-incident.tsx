import * as React from "react";

import { ConfidenceBar } from "@/components/confidence-bar";
import { DataTable, type ColumnDef } from "@/components/data-table";
import type { EvalPerIncident } from "@/components/evaluation/evaluation-metrics";
import { Badge } from "@/components/ui/badge";

const columns: ColumnDef<EvalPerIncident>[] = [
  {
    key: "incident",
    header: "Incident",
    render: (row) => (
      <span className="font-mono text-xs tabular-nums text-text-2">{row.incidentId}</span>
    ),
  },
  {
    key: "truth",
    header: "Ground truth",
    render: (row) => (
      <span className="font-mono text-xs text-text">{row.truth.replace(/_/g, " ")}</span>
    ),
  },
  {
    key: "predicted",
    header: "Predicted",
    render: (row) =>
      row.error ? (
        <span className="text-xs text-danger" title={row.error}>
          diagnosis error
        </span>
      ) : (
        <span className="font-mono text-xs text-text-2">
          {(row.predicted ?? "—").replace(/_/g, " ")}
        </span>
      ),
  },
  {
    key: "confidence",
    header: "Confidence",
    render: (row) =>
      row.confidence !== undefined ? (
        <ConfidenceBar value={row.confidence} />
      ) : (
        <span className="text-xs text-text-3">—</span>
      ),
  },
  {
    key: "top3",
    header: "Top-3",
    render: (row) =>
      row.top3.length > 0 ? (
        <span
          className="block max-w-[260px] truncate font-mono text-[10px] text-text-3"
          title={row.top3.join(", ")}
        >
          {row.top3.map((t) => t.replace(/_/g, " ")).join(" · ")}
        </span>
      ) : (
        <span className="text-xs text-text-3">—</span>
      ),
  },
  {
    key: "result",
    header: "Result",
    className: "text-right",
    render: (row) =>
      row.error ? (
        <Badge variant="danger">error</Badge>
      ) : row.correct === true ? (
        <Badge variant="success">top-1 match</Badge>
      ) : row.correct === false ? (
        <Badge variant="default">miss</Badge>
      ) : (
        <span className="text-xs text-text-3">—</span>
      ),
  },
];

/**
 * Per-incident diagnosis scoring exactly as stored in the run payload
 * (`arms.pulsecover.diagnosis.per_incident`) — truth vs predicted cause,
 * model confidence, and the top-3 candidate list.
 */
export function EvaluationPerIncident({ rows }: { rows: EvalPerIncident[] }) {
  return (
    <DataTable
      columns={columns}
      rows={rows}
      getRowId={(r) => r.incidentId}
      emptyTitle="No per-incident rows in this run"
      emptyDescription="The stored payload for this run carries no per-incident diagnosis breakdown."
    />
  );
}

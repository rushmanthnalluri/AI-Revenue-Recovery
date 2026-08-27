"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import type { IncidentSummary } from "@/lib/types";
import { formatINR, timeAgo } from "@/lib/format";
import { DataTable, type ColumnDef } from "@/components/data-table";
import { StatusPill } from "@/components/status-pill";

/** deviation_pct is stored in percent units (e.g. -14.08 ⇒ "-14.1%"). */
function formatDeviation(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${Math.abs(value).toFixed(1)}%`;
}

const columns: ColumnDef<IncidentSummary>[] = [
  {
    key: "incident",
    header: "Incident",
    render: (row) => (
      <div className="max-w-[340px]">
        <p className="truncate text-[13px] font-medium text-text">{row.title}</p>
        <p className="mt-0.5 truncate font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
          {row.metric} · {row.detection_method.replace(/_/g, " ")}
        </p>
      </div>
    ),
  },
  {
    key: "severity",
    header: "Severity",
    render: (row) => <StatusPill status={row.severity} />,
  },
  {
    key: "status",
    header: "Status",
    render: (row) => (
      <StatusPill
        status={row.status}
        pulse={row.status === "OPEN" || row.status === "RECOVERING"}
      />
    ),
  },
  {
    key: "deviation",
    header: "Deviation",
    className: "text-right",
    render: (row) => (
      <span className="font-mono text-xs tabular-nums text-text-2">
        {formatDeviation(row.deviation_pct)}
      </span>
    ),
  },
  {
    key: "risk",
    header: "Revenue at risk",
    className: "text-right",
    render: (row) => (
      <span className="font-mono text-xs tabular-nums text-text">
        {formatINR(row.revenue_at_risk_paise, { compact: true })}
      </span>
    ),
  },
  {
    key: "detected",
    header: "Detected",
    className: "text-right",
    render: (row) => (
      <span className="font-mono text-xs tabular-nums text-text-3">
        {timeAgo(row.detected_at)}
      </span>
    ),
  },
];

interface RecentIncidentsTableProps {
  incidents: IncidentSummary[] | undefined;
  loading: boolean;
}

/**
 * Recent degradation events — rows are keyboard-activatable (Enter/Space via
 * DataTable) and route to the incident detail screen.
 */
export function RecentIncidentsTable({ incidents, loading }: RecentIncidentsTableProps) {
  const router = useRouter();

  return (
    <DataTable
      columns={columns}
      rows={incidents}
      getRowId={(row) => row.id}
      isLoading={loading}
      emptyTitle="No incidents detected"
      emptyDescription="Detection has not flagged any payment degradation. Trigger a demo scenario below to watch the pipeline fire end-to-end."
      onRowClick={(row) => router.push(`/incidents/${row.id}`)}
      skeletonRows={4}
    />
  );
}

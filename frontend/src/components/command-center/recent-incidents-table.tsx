"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import type { IncidentSummary, Environment } from "@/lib/types";
import { formatINR, timeAgo } from "@/lib/format";
import { DataTable, type ColumnDef } from "@/components/data-table";
import { formatDeviation } from "@/components/incident/incident-metric";
import { StatusPill } from "@/components/status-pill";

const columns: ColumnDef<IncidentSummary>[] = [
  {
    key: "incident",
    header: "Incident",
    render: (row) => (
      <div className="max-w-[220px]">
        <p className="truncate text-[13px] font-medium text-text" title={row.title}>
          {row.title}
        </p>
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
  environment: Environment;
}

/**
 * Recent degradation events — rows are keyboard-activatable (Enter/Space via
 * DataTable) and route to the incident detail screen.
 */
export function RecentIncidentsTable({ incidents, loading, environment }: RecentIncidentsTableProps) {
  const router = useRouter();

  return (
    <DataTable
      columns={columns}
      rows={incidents}
      getRowId={(row) => row.id}
      isLoading={loading}
      emptyTitle="No incidents detected"
      emptyDescription={
        environment === "real_test"
          ? "Detection has not flagged any payment degradation. Once payments are observed from Razorpay Test Mode, anomalies appear here."
          : "Detection has not flagged any degradation in the research dataset. Run a scenario from the Research Lab to watch the pipeline fire end-to-end."
      }
      onRowClick={(row) => router.push(`/incidents/${row.id}`)}
      skeletonRows={4}
    />
  );
}

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { IncidentStatus, IncidentSummary, Severity } from "@/lib/types";
import { formatINR, formatNumber, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";
import { DataTable, type ColumnDef } from "@/components/data-table";
import { useEnvironment } from "@/components/environment-provider";
import { ErrorPanel } from "@/components/error-panel";
import { PageHeader } from "@/components/page-header";
import { ProvenanceChip } from "@/components/provenance";
import { SectionCard } from "@/components/section-card";
import { StatusPill } from "@/components/status-pill";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import {
  KNOWN_METRICS,
  formatDeviation,
  metricDirection,
  metricLabel,
} from "@/components/incident/incident-metric";

const PAGE_SIZE = 20;

const STATUSES: IncidentStatus[] = [
  "OPEN",
  "INVESTIGATING",
  "DIAGNOSED",
  "RECOVERING",
  "RESOLVED",
  "CLOSED",
  "FALSE_POSITIVE",
];
const SEVERITIES: Severity[] = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];

const columns: ColumnDef<IncidentSummary>[] = [
  {
    key: "incident",
    header: "Incident",
    render: (row) => (
      <div className="max-w-[380px]">
        <p className="truncate text-sm font-medium text-text">{row.title}</p>
        <p className="font-mono text-2xs tabular-nums text-text-3">
          {row.id} · {row.detection_method}
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
    render: (row) => <StatusPill status={row.status} pulse={row.status === "INVESTIGATING"} />,
  },
  {
    key: "metric",
    header: "Metric",
    render: (row) => (
      <span className="font-mono text-xs text-text-2" title={metricLabel(row.metric)}>
        {row.metric}
      </span>
    ),
  },
  {
    key: "deviation",
    header: "Deviation",
    className: "text-right",
    render: (row) => {
      const degraded =
        row.deviation_pct !== null &&
        row.deviation_pct !== undefined &&
        (metricDirection(row.metric) === "down"
          ? row.deviation_pct < 0
          : row.deviation_pct > 0);
      return (
        <span
          className={cn(
            "font-mono text-xs tabular-nums",
            row.deviation_pct === null || row.deviation_pct === undefined
              ? "text-text-3"
              : degraded
                ? "text-danger"
                : "text-success",
          )}
        >
          {formatDeviation(row.deviation_pct)}
        </span>
      );
    },
  },
  {
    key: "risk",
    header: "Revenue at risk",
    className: "text-right",
    render: (row) => (
      <span className="font-mono text-xs tabular-nums text-text">
        {formatINR(row.revenue_at_risk_paise)}
      </span>
    ),
  },
  {
    key: "detected",
    header: "Detected",
    className: "text-right",
    render: (row) => (
      <span className="font-mono text-xs tabular-nums text-text-3" title={row.detected_at}>
        {timeAgo(row.detected_at)}
      </span>
    ),
  },
];

/** /incidents — filterable, paginated register of the environment's incidents. */
export function IncidentListView() {
  const router = useRouter();
  const { environment } = useEnvironment();
  const [status, setStatus] = React.useState<IncidentStatus | "">("");
  const [severity, setSeverity] = React.useState<Severity | "">("");
  const [metric, setMetric] = React.useState<string>("");
  const [page, setPage] = React.useState(1);

  // Switching environments is a different dataset — restart pagination.
  React.useEffect(() => {
    setPage(1);
  }, [environment]);

  const query = useQuery({
    queryKey: ["incidents", "list", environment, { status, severity, metric, page }],
    queryFn: () =>
      api.incidents.list({
        status: status || null,
        severity: severity || null,
        metric: metric || null,
        environment,
        page,
        page_size: PAGE_SIZE,
      }),
    placeholderData: keepPreviousData,
  });

  const totalPages = query.data
    ? Math.max(1, Math.ceil(query.data.total / query.data.page_size))
    : 1;
  const hasFilters = status !== "" || severity !== "" || metric !== "";

  return (
    <div className="space-y-6">
      <PageHeader
        title="Incidents"
        description={
          environment === "real_test"
            ? "Detected payment degradation events from your Razorpay Test Mode activity, from anomaly to resolution."
            : "Detected degradation events in the synthetic research dataset, from anomaly to resolution."
        }
        actions={<ProvenanceChip environment={environment} records={query.data?.total} />}
      />

      <SectionCard
        title="All incidents"
        description="Filter by lifecycle status, severity, or the metric that degraded."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Select
              aria-label="Filter by status"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value as IncidentStatus | "");
                setPage(1);
              }}
              className="h-8 w-40 text-xs"
            >
              <option value="">All statuses</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s.replace(/_/g, " ")}
                </option>
              ))}
            </Select>
            <Select
              aria-label="Filter by severity"
              value={severity}
              onChange={(e) => {
                setSeverity(e.target.value as Severity | "");
                setPage(1);
              }}
              className="h-8 w-36 text-xs"
            >
              <option value="">All severities</option>
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
            <Select
              aria-label="Filter by metric"
              value={metric}
              onChange={(e) => {
                setMetric(e.target.value);
                setPage(1);
              }}
              className="h-8 w-48 text-xs"
            >
              <option value="">All metrics</option>
              {KNOWN_METRICS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </Select>
            {hasFilters ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setStatus("");
                  setSeverity("");
                  setMetric("");
                  setPage(1);
                }}
              >
                Reset
              </Button>
            ) : null}
          </div>
        }
        contentClassName="pt-0"
      >
        {query.isError ? (
          <ErrorPanel error={query.error} onRetry={() => query.refetch()} />
        ) : (
          <>
            <DataTable
              columns={columns}
              rows={query.data?.items}
              getRowId={(r) => r.id}
              isLoading={query.isPending}
              emptyTitle={hasFilters ? "No incidents match these filters" : "No incidents detected"}
              emptyDescription={
                hasFilters
                  ? "Try widening the status, severity, or metric filters."
                  : environment === "real_test"
                    ? "When the detection engine flags a degradation in your observed Razorpay Test Mode activity, it will appear here."
                    : "When the detection engine flags a degradation in the research dataset, it will appear here. Run a scenario from the Research Lab to generate one."
              }
              onRowClick={(row) => router.push(`/incidents/${row.id}`)}
            />
            <div className="mt-4 flex flex-wrap items-center justify-between gap-2 text-xs text-text-3">
              <span className="font-mono tabular-nums" aria-live="polite">
                {query.data
                  ? `${formatNumber(query.data.total)} total · page ${query.data.page} of ${totalPages}`
                  : "—"}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= totalPages}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        )}
      </SectionCard>
    </div>
  );
}

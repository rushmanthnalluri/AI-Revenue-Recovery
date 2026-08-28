"use client";

import * as React from "react";
import Link from "next/link";
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { OpportunitySummary, RecoveryStatus } from "@/lib/types";
import { formatINR, formatNumber, timeAgo } from "@/lib/format";
import { ConfidenceBar } from "@/components/confidence-bar";
import { DataTable, type ColumnDef } from "@/components/data-table";
import { ErrorPanel } from "@/components/error-panel";
import { SectionCard } from "@/components/section-card";
import { StatusPill } from "@/components/status-pill";
import { Button, buttonVariants } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  detailFailureClass,
  failureClassLabel,
  opportunityTypeLabel,
  parseOpportunityDetail,
} from "@/components/recovery/recovery-contract";
import { BuildFromIncidentAction } from "@/components/recovery/build-from-incident-action";
import { OpportunityDrawer } from "@/components/recovery/opportunity-drawer";

const PAGE_SIZE = 20;

const STATUSES: RecoveryStatus[] = [
  "PROPOSED",
  "POLICY_EVALUATED",
  "PENDING_APPROVAL",
  "APPROVED",
  "REJECTED",
  "EXECUTING",
  "VERIFYING",
  "RECOVERED",
  "FAILED",
  "UNKNOWN",
  "CANCELLED",
  "ESCALATED",
];

/** Opportunity types emitted by the backend OpportunityBuilder. */
const OPPORTUNITY_TYPES = ["failed_payment_retry", "dropped_checkout", "stuck_checkout_payment"];

/**
 * Failure class is not part of the opportunity-summary contract — the
 * strategy generator derives it per opportunity and records it on the
 * opportunity's audit trail. This cell reads it from the detail endpoint with
 * a 60s stale window so the table does not hammer the API. Shares its query
 * key with the drawer and approval cards, so opening a row never refetches.
 */
function FailureClassCell({ opportunityId }: { opportunityId: string }) {
  const detail = useQuery({
    queryKey: ["recovery", "detail", opportunityId],
    queryFn: () => api.recovery.get(opportunityId).then(parseOpportunityDetail),
    staleTime: 60_000,
    retry: 0,
  });
  if (detail.isPending) return <Skeleton className="h-4 w-20" />;
  const failureClass = detailFailureClass(detail.data);
  if (!failureClass) return <span className="text-xs text-text-3">—</span>;
  return (
    <span className="font-mono text-2xs uppercase tracking-[0.07em] text-text-2">
      {failureClassLabel(failureClass)}
    </span>
  );
}

const columns: ColumnDef<OpportunitySummary>[] = [
  {
    key: "opportunity",
    header: "Opportunity",
    render: (row) => (
      <div className="max-w-[260px]">
        <p className="truncate text-sm font-medium text-foreground">
          {opportunityTypeLabel(row.opportunity_type)}
        </p>
        <p className="truncate text-2xs text-muted-foreground tnum">
          {row.id}
          {row.incident_id ? ` · ${row.incident_id}` : ""}
        </p>
      </div>
    ),
  },
  {
    key: "customer",
    header: "Customer",
    render: (row) => (
      <div className="max-w-[180px]">
        <p className="truncate font-mono text-xs text-text-2">{row.customer_id ?? "—"}</p>
        {row.payment_id ? (
          <p className="truncate font-mono text-2xs text-text-3">{row.payment_id}</p>
        ) : null}
      </div>
    ),
  },
  {
    key: "amount",
    header: "Amount",
    className: "text-right",
    render: (row) => <span className="tnum text-sm">{formatINR(row.amount_paise)}</span>,
  },
  {
    key: "expected",
    header: "Expected recovery",
    className: "text-right",
    render: (row) => (
      <span className="tnum text-sm text-success">{formatINR(row.expected_recovery_paise)}</span>
    ),
  },
  {
    key: "failure-class",
    header: "Failure class",
    render: (row) => <FailureClassCell opportunityId={row.id} />,
  },
  {
    key: "confidence",
    header: "Confidence",
    render: (row) => <ConfidenceBar value={row.confidence} />,
  },
  {
    key: "risk",
    header: "Risk",
    render: (row) => <span className="text-xs capitalize text-muted-foreground">{row.risk}</span>,
  },
  {
    key: "status",
    header: "Status",
    render: (row) => <StatusPill status={row.status} />,
  },
  {
    key: "created",
    header: "Created",
    className: "text-right text-muted-foreground",
    render: (row) => <span className="text-xs tnum">{timeAgo(row.created_at)}</span>,
  },
];

export function PipelinePanel() {
  const [status, setStatus] = React.useState<RecoveryStatus | "">("");
  const [type, setType] = React.useState("");
  const [page, setPage] = React.useState(1);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);

  const query = useQuery({
    queryKey: ["recovery", "opportunities", "pipeline", status, type, page],
    queryFn: () =>
      api.recovery.opportunities({
        status: status || null,
        opportunity_type: type || null,
        page,
        page_size: PAGE_SIZE,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: 20_000,
  });

  const totalPages = query.data
    ? Math.max(1, Math.ceil(query.data.total / query.data.page_size))
    : 1;

  return (
    <>
      <SectionCard
        title="Recovery pipeline"
        description="Every opportunity end to end. Status is projected from the latest recovery action — webhook reconciliation updates actions directly."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Select
              aria-label="Filter by status"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value as RecoveryStatus | "");
                setPage(1);
              }}
              className="h-8 w-44 text-xs"
            >
              <option value="">All statuses</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s.replace(/_/g, " ")}
                </option>
              ))}
            </Select>
            <Select
              aria-label="Filter by opportunity type"
              value={type}
              onChange={(e) => {
                setType(e.target.value);
                setPage(1);
              }}
              className="h-8 w-48 text-xs"
            >
              <option value="">All types</option>
              {OPPORTUNITY_TYPES.map((t) => (
                <option key={t} value={t}>
                  {opportunityTypeLabel(t)}
                </option>
              ))}
            </Select>
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
              skeletonRows={6}
              onRowClick={(row) => setSelectedId(row.id)}
              emptyTitle="No recovery opportunities"
              emptyDescription="Opportunities are built from an open incident's failed payments and dropped checkouts — the build is idempotent and safe to re-run."
            />
            {query.data && query.data.items.length === 0 ? (
              <div className="mt-3 flex justify-center">
                {!status && !type ? (
                  <BuildFromIncidentAction />
                ) : (
                  <Link href="/" className={buttonVariants({ variant: "secondary", size: "sm" })}>
                    Open the Command Center
                  </Link>
                )}
              </div>
            ) : null}
            <div className="mt-4 flex items-center justify-between text-xs text-muted-foreground">
              <span className="tnum">
                {query.data
                  ? `${formatNumber(query.data.total)} total · page ${query.data.page} of ${totalPages}`
                  : "—"}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => p - 1)}
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

      <OpportunityDrawer opportunityId={selectedId} onClose={() => setSelectedId(null)} />
    </>
  );
}

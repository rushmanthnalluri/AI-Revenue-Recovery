"use client";

import * as React from "react";
import Link from "next/link";
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { PaymentSummary } from "@/lib/types";
import { formatINR, formatNumber, timeAgo } from "@/lib/format";
import { useEnvironment } from "@/components/environment-provider";
import { DataTable, type ColumnDef } from "@/components/data-table";
import { ErrorPanel } from "@/components/error-panel";
import { PageHeader } from "@/components/page-header";
import { ProvenanceChip, SourceTypeBadge } from "@/components/provenance";
import { SectionCard } from "@/components/section-card";
import { StatusPill } from "@/components/status-pill";
import { Button, buttonVariants } from "@/components/ui/button";
import { Select } from "@/components/ui/select";

const PAGE_SIZE = 20;

/** Razorpay payment lifecycle states (lowercase, as stored from gateway payloads). */
const PAYMENT_STATUSES = ["created", "authorized", "captured", "failed", "refunded"] as const;

/** Common Razorpay method facets; the filter is a server-side equality match. */
const PAYMENT_METHODS = ["card", "upi", "netbanking", "wallet", "emi", "paylater"] as const;

const columns: ColumnDef<PaymentSummary>[] = [
  {
    key: "payment",
    header: "Payment",
    render: (row) => (
      <div className="max-w-[220px]">
        <p className="truncate font-mono text-xs text-text" title={row.external_id ?? row.id}>
          {row.external_id ?? row.gateway_payment_id ?? row.id}
        </p>
        <p className="truncate font-mono text-2xs tabular-nums text-text-3" title={row.id}>
          {row.id}
        </p>
      </div>
    ),
  },
  {
    key: "order",
    header: "Order",
    render: (row) => (
      <span
        className="block max-w-[180px] truncate font-mono text-xs text-text-2"
        title={row.order_id ?? row.gateway_order_id ?? undefined}
      >
        {row.order_id ?? row.gateway_order_id ?? "—"}
      </span>
    ),
  },
  {
    key: "amount",
    header: "Amount",
    className: "text-right",
    render: (row) => (
      <span className="font-mono text-xs tabular-nums text-text">
        {formatINR(row.amount_paise)}
      </span>
    ),
  },
  {
    key: "method",
    header: "Method",
    render: (row) => (
      <span className="font-mono text-xs uppercase tracking-[0.05em] text-text-2">
        {row.method ?? "—"}
      </span>
    ),
  },
  {
    key: "status",
    header: "Status",
    render: (row) => <StatusPill status={row.status} />,
  },
  {
    key: "error",
    header: "Error reason",
    render: (row) =>
      row.error_code || row.error_description ? (
        <div className="max-w-[220px]">
          <p className="truncate font-mono text-2xs uppercase tracking-[0.05em] text-danger">
            {row.error_code ?? "error"}
          </p>
          {row.error_description ? (
            <p className="truncate text-2xs text-text-3" title={row.error_description}>
              {row.error_description}
            </p>
          ) : null}
        </div>
      ) : (
        <span className="text-xs text-text-3">—</span>
      ),
  },
  {
    key: "source",
    header: "Source",
    render: (row) => <SourceTypeBadge sourceType={row.source_type} />,
  },
  {
    key: "created",
    header: "Created",
    className: "text-right",
    render: (row) => (
      <span className="font-mono text-xs tabular-nums text-text-3" title={row.created_at}>
        {timeAgo(row.created_at)}
      </span>
    ),
  },
];

/**
 * /payments — the env-scoped observed payment register. Real merchant mode
 * lists rows synced from Razorpay Test Mode; Research Lab mode lists the
 * synthetic dataset. Provenance is a first-class column, not a tooltip.
 */
export function PaymentsView() {
  const { environment } = useEnvironment();
  const [status, setStatus] = React.useState("");
  const [method, setMethod] = React.useState("");
  const [page, setPage] = React.useState(1);

  // Switching environments is a different dataset — restart pagination.
  React.useEffect(() => {
    setPage(1);
  }, [environment]);

  const query = useQuery({
    queryKey: ["payments", "list", environment, status, method, page],
    queryFn: () =>
      api.payments.list({
        environment,
        status: status || null,
        method: method || null,
        page,
        page_size: PAGE_SIZE,
      }),
    placeholderData: keepPreviousData,
  });

  const totalPages = query.data
    ? Math.max(1, Math.ceil(query.data.total / query.data.page_size))
    : 1;
  const hasFilters = status !== "" || method !== "";

  const emptyCopy = hasFilters
    ? {
        title: "No payments match these filters",
        description: "Try widening the status or method filter.",
      }
    : environment === "real_test"
      ? {
          title: "No payments observed yet",
          description:
            "Connect Razorpay Test Mode and sync, or process a test payment — every observed payment lands here with its gateway fields.",
        }
      : {
          title: "No synthetic payments yet",
          description:
            "The Research Lab dataset is empty. Run a scenario to seed synthetic payment traffic for evaluation.",
        };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Payments"
        description={
          environment === "real_test"
            ? "Every payment observed from your Razorpay Test Mode account."
            : "Synthetic payment rows in the Research Lab dataset."
        }
        actions={<ProvenanceChip environment={environment} records={query.data?.total} />}
      />

      <SectionCard
        title="Observed payments"
        description="Gateway-shaped rows — external ids, amounts in INR, method, status and failure detail."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Select
              aria-label="Filter by status"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setPage(1);
              }}
              className="h-8 w-36 text-xs"
            >
              <option value="">All statuses</option>
              {PAYMENT_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
            <Select
              aria-label="Filter by method"
              value={method}
              onChange={(e) => {
                setMethod(e.target.value);
                setPage(1);
              }}
              className="h-8 w-40 text-xs"
            >
              <option value="">All methods</option>
              {PAYMENT_METHODS.map((m) => (
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
                  setMethod("");
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
              emptyTitle={emptyCopy.title}
              emptyDescription={emptyCopy.description}
            />
            {query.data && query.data.items.length === 0 && !hasFilters ? (
              <div className="mt-3 flex justify-center">
                <Link
                  href={environment === "real_test" ? "/settings" : "/research"}
                  className={buttonVariants({ variant: "secondary", size: "sm" })}
                >
                  {environment === "real_test" ? "Open Settings" : "Open Research Lab"}
                </Link>
              </div>
            ) : null}
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

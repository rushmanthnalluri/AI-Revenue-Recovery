"use client";

import * as React from "react";
import { useSearchParams } from "next/navigation";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, ScrollText } from "lucide-react";

import { api } from "@/lib/api";
import type { Environment } from "@/lib/types";
import { formatNumber } from "@/lib/format";
import { AuditTimeline } from "@/components/audit/audit-timeline";
import { AuditVerifyAction } from "@/components/audit/audit-verify-action";
import { EmptyState } from "@/components/empty-state";
import { useEnvironment } from "@/components/environment-provider";
import { ErrorPanel } from "@/components/error-panel";
import { PageHeader } from "@/components/page-header";
import { SectionCard } from "@/components/section-card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";

const PAGE_SIZE = 50;

/** Entity types the backend writes into `audit_logs` (services/policy/audit.py callers). */
const ENTITY_TYPES = [
  "incident",
  "recovery_opportunity",
  "recovery_action",
  "policy_decision",
  "agent_report",
  "diagnosis",
  "demo_environment",
] as const;

function TimelineSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading audit trail">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="flex gap-3">
          <Skeleton className="size-[27px] shrink-0 rounded-md" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-3 w-2/3" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function AuditView() {
  const { environment } = useEnvironment();
  const searchParams = useSearchParams();
  const [entityType, setEntityType] = React.useState("");
  // Deep links (`/audit?entity_id=act_…`, e.g. from the Approval Center)
  // pre-fill the entity-id filter — an exact server-side match.
  const [entityId, setEntityId] = React.useState(searchParams.get("entity_id") ?? "");
  // Environment scope — follows the global environment until changed locally.
  const [envFilter, setEnvFilter] = React.useState<Environment>(environment);
  const [page, setPage] = React.useState(1);

  React.useEffect(() => {
    setEnvFilter(environment);
    setPage(1);
  }, [environment]);

  const query = useQuery({
    queryKey: ["audit", "list", envFilter, entityType, entityId, page],
    queryFn: () =>
      api.audit.list({
        entity_type: entityType || null,
        entity_id: entityId || null,
        environment: envFilter,
        page,
        page_size: PAGE_SIZE,
      }),
    placeholderData: keepPreviousData,
    // The trail is append-only and busy during incidents — keep page 1 fresh.
    refetchInterval: 20_000,
  });

  const totalPages = query.data
    ? Math.max(1, Math.ceil(query.data.total / query.data.page_size))
    : 1;
  const filtered = entityType !== "" || entityId !== "";

  return (
    <div className="space-y-6">
      <PageHeader
        title="Audit Trail"
        description="Append-only record of every state transition — actor, entity, and request id, newest first. Scoped by environment: research rows (scenario runs, dataset resets) never mix into the real merchant trail."
      />

      <SectionCard
        title="Event stream"
        description="Chronological, immutable log rows — raw JSON details preserved"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Select
              value={envFilter}
              onChange={(e) => {
                setEnvFilter(e.target.value as Environment);
                setPage(1);
              }}
              aria-label="Filter by environment"
              className="h-7 w-auto px-2 text-xs"
            >
              <option value="real_test">Real Test (Razorpay)</option>
              <option value="research">Research (synthetic)</option>
            </Select>
            <Select
              value={entityType}
              onChange={(e) => {
                setEntityType(e.target.value);
                setPage(1);
              }}
              aria-label="Filter by entity type"
              className="h-7 w-auto px-2 text-xs"
            >
              <option value="">All entity types</option>
              {ENTITY_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </Select>
            <Input
              value={entityId}
              onChange={(e) => {
                setEntityId(e.target.value);
                setPage(1);
              }}
              placeholder="entity id — inc_…, act_…, pol_…"
              aria-label="Filter by entity id"
              className="h-7 w-52 px-2 text-xs"
            />
            <AuditVerifyAction />
          </div>
        }
        contentClassName="pt-0"
      >
        {query.isPending ? (
          <TimelineSkeleton />
        ) : query.isError ? (
          <ErrorPanel error={query.error} onRetry={() => query.refetch()} />
        ) : query.data.items.length === 0 ? (
          <EmptyState
            icon={ScrollText}
            title={filtered ? "No entries match these filters" : "No audit entries yet"}
            description={
              filtered
                ? "Widen the entity type or clear the entity id filter to see more of the trail."
                : envFilter === "real_test"
                  ? "Actions on your observed Razorpay Test Mode activity are recorded here as they happen."
                  : "Every research action — scenario runs, dataset resets, synthetic recovery transitions — is recorded here as it happens."
            }
          />
        ) : (
          <>
            <AuditTimeline entries={query.data.items} />
            <div className="mt-5 flex items-center justify-between border-t border-border pt-4 text-xs text-text-3">
              <span className="font-mono tabular-nums">
                {formatNumber(query.data.total)} {query.data.total === 1 ? "event" : "events"} ·
                page {query.data.page} of {totalPages}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  aria-label="Previous audit page"
                >
                  <ChevronLeft className="size-3.5" aria-hidden />
                  Previous
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={page >= totalPages}
                  onClick={() => setPage((p) => p + 1)}
                  aria-label="Next audit page"
                >
                  Next
                  <ChevronRight className="size-3.5" aria-hidden />
                </Button>
              </div>
            </div>
          </>
        )}
      </SectionCard>
    </div>
  );
}

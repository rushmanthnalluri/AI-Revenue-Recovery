"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { PipelinePanel } from "@/components/recovery/pipeline-panel";
import { ApprovalsPanel } from "@/components/recovery/approvals-panel";

type Tab = "pipeline" | "approvals";

const TABS: { id: Tab; label: string }[] = [
  { id: "pipeline", label: "Pipeline" },
  { id: "approvals", label: "Approval center" },
];

/**
 * Recovery console — pipeline of policy-gated opportunities plus the human
 * Approval Center. AI proposes, the deterministic policy engine decides, a
 * human handles the edge cases.
 */
export function RecoveryPlannerView() {
  const [tab, setTab] = React.useState<Tab>("pipeline");
  const tabRefs = React.useRef<(HTMLButtonElement | null)[]>([]);

  // Same key as the ApprovalsPanel queue — one shared fetch feeds the badge.
  const pending = useQuery({
    queryKey: ["recovery", "opportunities", "pending-approval"],
    queryFn: () =>
      api.recovery.opportunities({ status: "PENDING_APPROVAL", page: 1, page_size: 50 }),
    refetchInterval: 10_000,
  });
  const pendingCount = pending.data?.total ?? 0;

  const onTabKeyDown = (event: React.KeyboardEvent, index: number) => {
    let next: number | null = null;
    if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + TABS.length) % TABS.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = TABS.length - 1;
    if (next === null) return;
    event.preventDefault();
    const target = TABS[next];
    if (!target) return;
    setTab(target.id);
    tabRefs.current[next]?.focus();
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Recovery"
        description="Policy-gated revenue recovery — AI proposes and ranks strategies, the deterministic policy engine decides, humans settle the edge cases."
      />

      <div
        role="tablist"
        aria-label="Recovery views"
        className="flex gap-1 rounded-lg border border-border bg-surface p-1"
      >
        {TABS.map(({ id, label }, index) => (
          <button
            key={id}
            ref={(el) => {
              tabRefs.current[index] = el;
            }}
            role="tab"
            id={`recovery-tab-${id}`}
            aria-selected={tab === id}
            aria-controls={`recovery-panel-${id}`}
            tabIndex={tab === id ? 0 : -1}
            onClick={() => setTab(id)}
            onKeyDown={(e) => onTabKeyDown(e, index)}
            className={cn(
              "flex items-center gap-2 rounded-md px-3 py-1.5 text-[13px] transition-colors duration-150 ease-apple",
              tab === id
                ? "bg-raised font-medium text-text shadow-[inset_0_-2px_0_#D9A63F]"
                : "text-text-2 hover:bg-raised hover:text-text",
            )}
          >
            {label}
            {id === "approvals" && pendingCount > 0 ? (
              <Badge variant="warning">{pendingCount}</Badge>
            ) : null}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id={`recovery-panel-${tab}`}
        aria-labelledby={`recovery-tab-${tab}`}
      >
        {tab === "pipeline" ? <PipelinePanel /> : <ApprovalsPanel />}
      </div>
    </div>
  );
}

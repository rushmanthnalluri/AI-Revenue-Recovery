"use client";

import * as React from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { FlaskConical } from "lucide-react";

import { cn } from "@/lib/utils";
import { DemoControl } from "@/components/demo-control";
import { PageHeader } from "@/components/page-header";
import { EvaluationView } from "@/components/evaluation/evaluation-view";

type Tab = "scenarios" | "evaluation";

const TABS: { id: Tab; label: string }[] = [
  { id: "scenarios", label: "Scenarios" },
  { id: "evaluation", label: "Evaluation" },
];

function tabFromParam(value: string | null): Tab {
  return value === "evaluation" ? "evaluation" : "scenarios";
}

/**
 * Research Lab — the isolated simulator workspace. Houses the scenario
 * runner (synthetic dataset seeding) and the evaluation harness as tabs,
 * under an explicit header that says exactly what this data is. Nothing on
 * this page is merchant data; the real merchant environment reads are
 * scoped away from everything produced here.
 */
export function ResearchView() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const tab = tabFromParam(searchParams.get("tab"));
  const tabRefs = React.useRef<(HTMLButtonElement | null)[]>([]);

  const setTab = React.useCallback(
    (next: Tab) => {
      const params = new URLSearchParams(searchParams.toString());
      if (next === "scenarios") params.delete("tab");
      else params.set("tab", next);
      const qs = params.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [pathname, router, searchParams],
  );

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
        title="Research Lab"
        description="Controlled scenarios and ML evaluation on the synthetic dataset."
      />

      {/* Research SIMULATOR disclosure — always visible on this page */}
      <div
        role="note"
        className="flex items-start gap-3 rounded-lg border border-[rgba(110,143,160,0.45)] bg-info-dim px-4 py-3.5"
      >
        <FlaskConical aria-hidden className="mt-0.5 size-4 shrink-0 text-info" strokeWidth={1.5} />
        <p className="text-[13px] text-text-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.09em] text-info">
            Research simulator —{" "}
          </span>
          synthetic data only; used for ML evaluation and controlled incident testing; not
          merchant data.
        </p>
      </div>

      <div
        role="tablist"
        aria-label="Research lab views"
        className="flex gap-1 rounded-lg border border-border bg-surface p-1"
      >
        {TABS.map(({ id, label }, index) => (
          <button
            key={id}
            ref={(el) => {
              tabRefs.current[index] = el;
            }}
            role="tab"
            id={`research-tab-${id}`}
            aria-selected={tab === id}
            aria-controls={`research-panel-${id}`}
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
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`research-panel-${tab}`} aria-labelledby={`research-tab-${tab}`}>
        {tab === "scenarios" ? <DemoControl /> : <EvaluationView embedded />}
      </div>
    </div>
  );
}

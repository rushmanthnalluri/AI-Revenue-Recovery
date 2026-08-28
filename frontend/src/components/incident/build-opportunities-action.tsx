"use client";

import * as React from "react";
import Link from "next/link";
import { CheckCircle2, Hammer, Loader2, X } from "lucide-react";

import { formatINR, formatNumber } from "@/lib/format";
import { ErrorPanel } from "@/components/error-panel";
import { Button } from "@/components/ui/button";
import { useBuildOpportunities } from "@/components/recovery/recovery-hooks";

/**
 * "Build recovery opportunities" — the incident-detail action that closes
 * the old curl-only path (POST /api/v1/recovery/opportunities/build).
 *
 * Two-step confirm: the first click arms the action and shows the
 * idempotency note, the second runs the build. On success the real
 * BuildResponse summary (created / already existed / paise in scope) is
 * rendered, and the shared hook invalidates recovery + incident queries.
 * Escape or Cancel disarms; keyboard focus follows the steps. Rendered by
 * the detail view only while the incident is open (non-terminal).
 */
export function BuildOpportunitiesAction({ incidentId }: { incidentId: string }) {
  const [armed, setArmed] = React.useState(false);
  const [dismissed, setDismissed] = React.useState(false);
  const rootRef = React.useRef<HTMLDivElement>(null);
  const wasArmed = React.useRef(false);
  const mutation = useBuildOpportunities();

  /* Focus follows the step: arming moves to Confirm, disarming (Cancel,
     Escape, or a finished run) returns to the trigger. (ui/Button does not
     forward refs, so resolve the buttons through the container.) */
  React.useEffect(() => {
    const root = rootRef.current;
    if (armed && !wasArmed.current) {
      root?.querySelector<HTMLButtonElement>("[data-build-confirm]")?.focus();
    }
    if (!armed && wasArmed.current) {
      root?.querySelector<HTMLButtonElement>("[data-build-trigger]")?.focus();
    }
    wasArmed.current = armed;
  }, [armed]);

  const result = dismissed ? null : (mutation.data ?? null);
  const inScopePaise = result
    ? result.opportunities.reduce((sum, opp) => sum + opp.amount_paise, 0)
    : 0;

  return (
    <div ref={rootRef} className="space-y-2">
      <div
        className="flex flex-wrap items-center justify-end gap-2"
        onKeyDown={(event) => {
          if (event.key === "Escape" && armed && !mutation.isPending) {
            event.stopPropagation();
            setArmed(false);
          }
        }}
      >
        {armed ? (
          <>
            <span className="font-mono text-2xs text-text-3">
              idempotent — existing opportunities are reused, never duplicated
            </span>
            <Button
              data-build-confirm
              size="sm"
              disabled={mutation.isPending}
              onClick={() =>
                mutation.mutate(incidentId, { onSettled: () => setArmed(false) })
              }
            >
              {mutation.isPending ? (
                <Loader2 className="size-4 animate-spin" strokeWidth={1.5} aria-hidden />
              ) : (
                <Hammer className="size-4" strokeWidth={1.5} aria-hidden />
              )}
              {mutation.isPending ? "Building…" : "Confirm build"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={mutation.isPending}
              onClick={() => setArmed(false)}
            >
              Cancel
            </Button>
          </>
        ) : (
          <Button
            data-build-trigger
            size="sm"
            onClick={() => {
              setDismissed(false);
              setArmed(true);
            }}
          >
            <Hammer className="size-4" strokeWidth={1.5} aria-hidden />
            Build recovery opportunities
          </Button>
        )}
      </div>

      {mutation.isError ? (
        <ErrorPanel
          error={mutation.error}
          title="Could not build opportunities"
          onRetry={() => {
            setDismissed(false);
            setArmed(true);
          }}
        />
      ) : null}

      {result ? (
        <div
          role="status"
          className="flex flex-wrap items-center justify-end gap-x-3 gap-y-1 rounded-lg border border-[rgba(111,191,140,0.35)] bg-success-dim px-4 py-2.5"
        >
          <p className="flex items-center gap-2 text-[13px] text-text-2">
            <CheckCircle2 className="size-4 text-success" strokeWidth={1.5} aria-hidden />
            <span>
              Build complete —{" "}
              <span className="tnum font-medium text-success">
                {formatNumber(result.created_count)} created
              </span>
              {" · "}
              <span className="tnum">{formatNumber(result.existing_count)} already existed</span>
              {inScopePaise > 0 ? (
                <span className="tnum"> · {formatINR(inScopePaise)} in scope</span>
              ) : null}
            </span>
          </p>
          <Link
            href="/recovery"
            className="font-mono text-[11px] uppercase tracking-[0.07em] text-accent transition-colors duration-150 ease-apple hover:text-accent-hover"
          >
            Open recovery pipeline →
          </Link>
          <button
            type="button"
            aria-label="Dismiss build result"
            onClick={() => setDismissed(true)}
            className="text-text-3 transition-colors duration-150 ease-apple hover:text-text"
          >
            <X className="size-3.5" strokeWidth={1.5} aria-hidden />
          </button>
        </div>
      ) : null}
    </div>
  );
}

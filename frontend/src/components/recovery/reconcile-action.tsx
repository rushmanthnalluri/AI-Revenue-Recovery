"use client";

import * as React from "react";
import { Loader2, RefreshCw } from "lucide-react";

import { formatNumber } from "@/lib/format";
import { ErrorPanel } from "@/components/error-panel";
import { Button } from "@/components/ui/button";
import { useReconcile } from "@/components/recovery/recovery-hooks";

/**
 * Run reconciliation — operator-triggered sweep (POST /recovery/reconcile,
 * ADR 0011). UNKNOWN actions are re-queried against gateway truth (GETs only,
 * never a blind retry) and failed webhook events are re-run through the live
 * handler registry. The sweep is mutating but idempotent, so it sits behind
 * the house two-step confirm; the report under the buttons is the real
 * response, never assumed counts.
 */
export function ReconcileAction() {
  const [confirming, setConfirming] = React.useState(false);
  const reconcile = useReconcile();
  const report = reconcile.data ?? null;

  return (
    <>
      {confirming ? (
        <>
          <Button
            size="sm"
            disabled={reconcile.isPending}
            onClick={() => reconcile.mutate(undefined, { onSuccess: () => setConfirming(false) })}
          >
            {reconcile.isPending ? (
              <Loader2 className="size-4 animate-spin" strokeWidth={1.5} aria-hidden />
            ) : (
              <RefreshCw className="size-4" strokeWidth={1.5} aria-hidden />
            )}
            {reconcile.isPending ? "Reconciling…" : "Confirm sweep"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={reconcile.isPending}
            onClick={() => setConfirming(false)}
          >
            Cancel
          </Button>
        </>
      ) : (
        <Button
          variant="outline"
          size="sm"
          title="Re-query every UNKNOWN action against gateway truth and reprocess failed webhooks (idempotent)"
          onClick={() => setConfirming(true)}
        >
          <RefreshCw className="size-4" strokeWidth={1.5} aria-hidden />
          Run reconciliation
        </Button>
      )}

      {reconcile.isError ? (
        <ErrorPanel
          error={reconcile.error}
          onRetry={() => reconcile.mutate()}
          title="Reconciliation failed"
          className="basis-full"
        />
      ) : null}

      {report && !reconcile.isPending ? (
        <div
          role="status"
          aria-live="polite"
          className="basis-full rounded-md border border-border bg-raised/40 px-3 py-2"
        >
          <p className="font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
            sweep <span className="normal-case text-text-2">{report.sweep_id}</span>
          </p>
          <p className="mt-0.5 text-xs text-text-2">
            <span className="tnum">{formatNumber(report.resolved)}</span> of{" "}
            <span className="tnum">{formatNumber(report.unknown_scanned)}</span> UNKNOWN
            resolved
            {report.still_unknown > 0 ? (
              <>
                {" · "}
                <span className="tnum">{formatNumber(report.still_unknown)}</span> still
                unknown
              </>
            ) : null}
            {" — "}
            <span className="tnum">{formatNumber(report.webhooks_reprocessed)}</span> webhooks
            reprocessed
            {report.webhooks_still_failing > 0 ? (
              <>
                {" · "}
                <span className="tnum">{formatNumber(report.webhooks_still_failing)}</span>{" "}
                still failing
              </>
            ) : null}
            .
          </p>
        </div>
      ) : null}
    </>
  );
}

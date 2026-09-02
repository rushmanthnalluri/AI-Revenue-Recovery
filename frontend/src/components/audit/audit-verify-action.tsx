"use client";

import * as React from "react";
import { Loader2, ShieldCheck, TriangleAlert } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { ErrorPanel } from "@/components/error-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

/**
 * Verify integrity — read-only full-chain verification of the hash-chained
 * audit trail (GET /api/v1/audit/verify). The chain spans BOTH environments
 * in insertion order (scoping would break linkage), so this button lives
 * above the env-scoped stream and ignores its filters. Read-only GET, so no
 * two-step confirm: armed by click, fired immediately. Valid gets a quiet
 * success strip; a broken chain gets a danger panel naming the first bad row.
 */
export function AuditVerifyAction() {
  const verify = useQuery({
    queryKey: ["audit", "verify"],
    queryFn: () => api.audit.verify(),
    enabled: false, // operator-triggered only — never polled
    retry: 0,
  });
  const report = verify.data ?? null;

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        disabled={verify.isFetching}
        title="Recompute digests and check linkage over the entire audit trail (read-only; spans both environments)"
        onClick={() => verify.refetch()}
      >
        {verify.isFetching ? (
          <Loader2 className="size-4 animate-spin" strokeWidth={1.5} aria-hidden />
        ) : (
          <ShieldCheck className="size-4" strokeWidth={1.5} aria-hidden />
        )}
        {verify.isFetching ? "Verifying…" : "Verify integrity"}
      </Button>

      {verify.isError ? (
        <ErrorPanel
          error={verify.error}
          onRetry={() => verify.refetch()}
          title="Verification request failed"
          className="basis-full"
        />
      ) : null}

      {report && !verify.isFetching ? (
        report.valid ? (
          <div
            role="status"
            aria-live="polite"
            className="basis-full rounded-md border border-border bg-raised/40 px-3 py-2"
          >
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="success">chain valid</Badge>
              <span className="font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
                <span className="tnum normal-case text-text-2">{formatNumber(report.checked)}</span>
                {" rows checked · "}
                <span className="tnum normal-case text-text-2">{formatNumber(report.chained)}</span>
                {" chained · "}
                <span className="tnum normal-case text-text-2">{formatNumber(report.legacy)}</span>
                {" legacy"}
              </span>
            </div>
            <p className="mt-1 text-[11px] text-text-3">
              Whole-chain verification across BOTH environments (Real Test + Research) — the
              hash chain links every row in insertion order, so it always covers more than the
              filtered stream above.
            </p>
          </div>
        ) : (
          <div
            role="alert"
            className="basis-full rounded-lg border border-[rgba(198,93,85,0.45)] bg-danger-dim px-4 py-3.5"
          >
            <div className="flex items-center gap-2 text-danger">
              <TriangleAlert className="size-4" strokeWidth={1.5} aria-hidden />
              <p className="text-[13.5px] font-semibold">Audit chain broken</p>
            </div>
            <p className="mt-1 text-[13px] text-text-2">
              Hash-chain verification failed — the trail no longer proves an unbroken,
              untampered record.
              {report.first_bad_id ? (
                <>
                  {" First failing row: "}
                  <span className="font-mono text-xs text-danger">{report.first_bad_id}</span>.
                </>
              ) : null}
            </p>
            <p className="mt-1 font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
              <span className="tnum normal-case text-text-2">{formatNumber(report.checked)}</span>
              {" rows checked · "}
              <span className="tnum normal-case text-text-2">{formatNumber(report.chained)}</span>
              {" chained · "}
              <span className="tnum normal-case text-text-2">{formatNumber(report.legacy)}</span>
              {" legacy"}
            </p>
            <p className="mt-1 text-[11px] text-text-3">
              Verification walks the whole chain across both environments (Real Test + Research),
              not just the filtered stream above.
            </p>
          </div>
        )
      ) : null}
    </>
  );
}

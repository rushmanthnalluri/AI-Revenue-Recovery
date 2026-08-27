import * as React from "react";
import { TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ApiError, toApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

interface ErrorPanelProps {
  error: unknown;
  onRetry?: () => void;
  title?: string;
  className?: string;
}

function describe(err: ApiError): { headline: string; detail: string } {
  if (err.isUnreachable) {
    return {
      headline: "Backend unreachable",
      detail:
        err.code === "timeout"
          ? "The API did not respond within 10 seconds."
          : "The PulseRecover API is not responding. Start the backend (uvicorn on :8000) and retry.",
    };
  }
  if (err.status === 401) {
    return {
      headline: "Unauthorized",
      detail: "Missing or invalid API key. Set NEXT_PUBLIC_API_KEY to match the backend API_KEY.",
    };
  }
  return { headline: `Request failed (${err.status})`, detail: err.message };
}

/** Error banner per the alerts recipe — danger hairline + dim wash, never
    fabricated data. */
export function ErrorPanel({ error, onRetry, title, className }: ErrorPanelProps) {
  const err = toApiError(error);
  const { headline, detail } = describe(err);

  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-start gap-3 rounded-lg border border-[rgba(198,93,85,0.45)] bg-danger-dim px-4 py-3.5",
        className,
      )}
    >
      <div className="flex items-center gap-2 text-danger">
        <TriangleAlert className="size-4" strokeWidth={1.5} aria-hidden />
        <p className="text-[13.5px] font-semibold">{title ?? headline}</p>
      </div>
      <p className="text-[13px] text-text-2">{detail}</p>
      <dl className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
        <div className="flex gap-1.5">
          <dt>code</dt>
          <dd className="normal-case text-text-2">{err.code}</dd>
        </div>
        {err.requestId ? (
          <div className="flex gap-1.5">
            <dt>request id</dt>
            <dd className="normal-case text-text-2">{err.requestId}</dd>
          </div>
        ) : null}
      </dl>
      {onRetry ? (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  );
}

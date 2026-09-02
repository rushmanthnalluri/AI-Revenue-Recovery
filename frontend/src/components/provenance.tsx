import * as React from "react";

import {
  ENVIRONMENT_BADGE_LABEL,
  environmentDataLabel,
  environmentForSourceType,
  sourceTypeLabel,
} from "@/lib/environment";
import { formatNumber } from "@/lib/format";
import type { Environment } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface ProvenanceChipProps {
  environment: Environment;
  /** Observation window, only when it comes from real response fields. */
  window?: string | null;
  /** Observed record count, only when it comes from real response fields. */
  records?: number | null;
  /**
   * Unit noun for the record count ("records" default). Pass "events" where
   * the count covers the payment-event stream rather than synced rows, so
   * two surfaces never claim the same noun for different semantics.
   */
  recordsLabel?: string;
  /** Extra provenance detail (e.g. a simulator run id already on the row). */
  detail?: string | null;
  className?: string;
}

/**
 * Small provenance chip pinned next to KPIs and detail headers. Real mode:
 * "Razorpay Test Mode · <window> · <n> records" (emerald). Research mode:
 * "Synthetic Research Dataset · <detail>" (slate). Text always carries the
 * meaning — colour is never the only signal.
 */
export function ProvenanceChip({
  environment,
  window,
  records,
  recordsLabel = "records",
  detail,
  className,
}: ProvenanceChipProps) {
  const parts: string[] = [environmentDataLabel(environment)];
  if (environment === "real_test") {
    if (window) parts.push(window);
    if (records !== null && records !== undefined) {
      parts.push(`${formatNumber(records)} ${recordsLabel}`);
    }
  } else if (detail) {
    parts.push(detail);
  }

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-sm border px-[7px] py-[3px] font-mono text-[9.5px] uppercase tracking-[0.07em]",
        environment === "real_test"
          ? "border-transparent bg-success-dim text-success"
          : "border-transparent bg-info-dim text-info",
        className,
      )}
    >
      {parts.join(" · ")}
    </span>
  );
}

/** Compact per-row environment badge (audit trail, pipelines). */
export function EnvironmentBadge({
  environment,
  className,
}: {
  environment?: Environment | string | null;
  className?: string;
}) {
  // Backend stamps `environment or "research"` on read — mirror that fallback.
  const env: Environment = environment === "real_test" ? "real_test" : "research";
  return (
    <Badge
      variant={env === "real_test" ? "success" : "info"}
      title={env === "real_test" ? "Razorpay Test Mode data" : "Synthetic research data"}
      className={className}
    >
      {ENVIRONMENT_BADGE_LABEL[env]}
    </Badge>
  );
}

/** Payment-row source badge: "razorpay test" / "razorpay live" / "research". */
export function SourceTypeBadge({
  sourceType,
  className,
}: {
  sourceType: string;
  className?: string;
}) {
  const env = environmentForSourceType(sourceType);
  return (
    <Badge
      variant={env === "real_test" ? "success" : "info"}
      title={`source_type: ${sourceType}`}
      className={className}
    >
      {sourceTypeLabel(sourceType)}
    </Badge>
  );
}

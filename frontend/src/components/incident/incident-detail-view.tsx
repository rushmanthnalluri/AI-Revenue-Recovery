"use client";

import * as React from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";

import { api } from "@/lib/api";
import type { IncidentDetail } from "@/lib/types";
import { formatDateTime, formatINR, formatNumber, timeAgo } from "@/lib/format";
import { ErrorPanel } from "@/components/error-panel";
import { MetricStrip, type MetricStripItem } from "@/components/metric-strip";
import { SectionCard } from "@/components/section-card";
import { StatusPill } from "@/components/status-pill";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  anomalyStartFrom,
  extractMetricSeries,
  IncidentMetricChart,
} from "@/components/incident/incident-metric-chart";
import {
  extractSegmentBreakdown,
  IncidentSegmentBreakdown,
} from "@/components/incident/incident-segment-breakdown";
import {
  extractInsights,
  IncidentInsightsPanel,
} from "@/components/incident/incident-insights";
import { IncidentDiagnosisCard } from "@/components/incident/incident-diagnosis-card";
import { IncidentAuditTimeline } from "@/components/incident/incident-audit-timeline";
import {
  formatDeviation,
  formatMetricValue,
  metricDirection,
  metricLabel,
} from "@/components/incident/incident-metric";
import { InvestigationPanel } from "@/components/investigation/investigation-panel";

function DetailSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading incident">
      <div className="space-y-2">
        <Skeleton className="h-3 w-32" />
        <Skeleton className="h-6 w-2/3" />
        <Skeleton className="h-3 w-1/2" />
      </div>
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-72 w-full" />
      <Skeleton className="h-40 w-full" />
    </div>
  );
}

function statBand(incident: IncidentDetail): MetricStripItem[] {
  const degraded =
    incident.deviation_pct !== null &&
    incident.deviation_pct !== undefined &&
    (metricDirection(incident.metric) === "down"
      ? incident.deviation_pct < 0
      : incident.deviation_pct > 0);

  const loss = incident.revenue?.observed_loss;
  const recoverable = incident.revenue?.recoverable;

  return [
    {
      key: "observed",
      label: `Observed ${metricLabel(incident.metric)}`,
      value: formatMetricValue(incident.metric, incident.observed_value),
      hint: `baseline ${formatMetricValue(incident.metric, incident.baseline_value)}`,
    },
    {
      key: "deviation",
      label: "Deviation",
      value: formatDeviation(incident.deviation_pct),
      tone:
        incident.deviation_pct === null || incident.deviation_pct === undefined
          ? "default"
          : degraded
            ? "danger"
            : "success",
      hint: metricDirection(incident.metric) === "down" ? "drop is degradation" : "rise is degradation",
    },
    {
      key: "affected",
      label: "Affected payments",
      value: formatNumber(incident.affected_payments_count),
      hint: `${incident.opportunities_count ?? 0} opportunities · ${incident.recovery_actions_count ?? 0} actions`,
    },
    {
      key: "risk",
      label: "Revenue at risk",
      value: formatINR(incident.revenue_at_risk_paise),
      tone: "danger",
      badge:
        loss?.low_confidence === true ? <Badge variant="warning">low confidence</Badge> : undefined,
      hint: loss
        ? `CI ${formatINR(loss.lower_paise)} – ${formatINR(loss.upper_paise)}`
        : undefined,
    },
    {
      key: "recoverable",
      label: "Recoverable",
      value:
        recoverable?.point_paise !== null && recoverable?.point_paise !== undefined
          ? formatINR(recoverable.point_paise)
          : "—",
      tone: "success",
      hint: recoverable
        ? `CI ${formatINR(recoverable.lower_paise)} – ${formatINR(recoverable.upper_paise)}`
        : undefined,
    },
  ];
}

/** /incidents/[id] — Incident Intelligence: stat band, evidence chart,
    segment localization, diagnosis, AI investigation and audit trail. */
export function IncidentDetailView({ incidentId }: { incidentId: string }) {
  const query = useQuery({
    queryKey: ["incidents", "detail", incidentId],
    queryFn: () => api.incidents.get(incidentId),
  });

  if (query.isPending) {
    return <DetailSkeleton />;
  }

  if (query.isError || !query.data) {
    return (
      <div className="space-y-6">
        <Link
          href="/incidents"
          className="inline-flex items-center gap-1.5 text-xs text-text-3 transition-colors duration-150 ease-apple hover:text-text"
        >
          <ArrowLeft className="size-3.5" strokeWidth={1.5} aria-hidden />
          All incidents
        </Link>
        <ErrorPanel
          error={query.error}
          title="Could not load this incident"
          onRetry={() => query.refetch()}
        />
      </div>
    );
  }

  const incident = query.data;
  const series = extractMetricSeries(incident.evidence);
  const segments = extractSegmentBreakdown(incident.evidence);
  const anomalyStart = anomalyStartFrom(incident.description, incident.detected_at);
  const segmentEntries = Object.entries(incident.segment ?? {});

  return (
    <div className="space-y-8">
      {/* header */}
      <header className="space-y-3">
        <Link
          href="/incidents"
          className="inline-flex items-center gap-1.5 text-xs text-text-3 transition-colors duration-150 ease-apple hover:text-text"
        >
          <ArrowLeft className="size-3.5" strokeWidth={1.5} aria-hidden />
          All incidents
        </Link>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.11em] text-text-3">
              <span aria-hidden className="inline-block h-px w-[18px] bg-accent" />
              Incident intelligence
            </p>
            <h1 className="mt-1.5 max-w-3xl text-lg font-semibold tracking-tight text-text">
              {incident.title}
            </h1>
          </div>
          <div className="flex items-center gap-2">
            <StatusPill status={incident.severity} />
            <StatusPill status={incident.status} pulse={incident.status === "INVESTIGATING"} />
          </div>
        </div>
        <dl className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-2xs tabular-nums text-text-3">
          <div className="flex gap-1.5">
            <dt className="uppercase tracking-[0.07em]">id</dt>
            <dd className="text-text-2">{incident.id}</dd>
          </div>
          <div className="flex gap-1.5">
            <dt className="uppercase tracking-[0.07em]">metric</dt>
            <dd className="text-text-2">{incident.metric}</dd>
          </div>
          <div className="flex gap-1.5">
            <dt className="uppercase tracking-[0.07em]">detector</dt>
            <dd className="text-text-2">{incident.detection_method}</dd>
          </div>
          <div className="flex gap-1.5">
            <dt className="uppercase tracking-[0.07em]">detected</dt>
            <dd className="text-text-2" title={incident.detected_at}>
              {formatDateTime(incident.detected_at)} ({timeAgo(incident.detected_at)})
            </dd>
          </div>
          {incident.window_start && incident.window_end ? (
            <div className="flex gap-1.5">
              <dt className="uppercase tracking-[0.07em]">window</dt>
              <dd className="text-text-2">
                {formatDateTime(incident.window_start)} → {formatDateTime(incident.window_end)}
              </dd>
            </div>
          ) : null}
          {incident.resolved_at ? (
            <div className="flex gap-1.5">
              <dt className="uppercase tracking-[0.07em]">resolved</dt>
              <dd className="text-text-2">{formatDateTime(incident.resolved_at)}</dd>
            </div>
          ) : null}
        </dl>
        {segmentEntries.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
              localized to
            </span>
            {segmentEntries.map(([key, value]) => (
              <Badge key={key} variant="outline">
                {key}={value}
              </Badge>
            ))}
          </div>
        ) : null}
        {incident.description ? (
          <p className="max-w-3xl text-xs leading-relaxed text-text-3">{incident.description}</p>
        ) : null}
        {incident.root_cause ? (
          <p className="max-w-3xl text-xs leading-relaxed text-text-2">
            <span className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
              root cause ·{" "}
            </span>
            {incident.root_cause}
          </p>
        ) : null}
      </header>

      {/* stat band */}
      <MetricStrip items={statBand(incident)} className="min-[1440px]:grid-cols-5" />

      {/* metric timeline */}
      <SectionCard
        title="Metric timeline"
        description={`Detector's bucketed ${incident.metric} series snapshot — baseline region shaded, dashed line marks the baseline level, marker marks the anomaly start.`}
      >
        {series ? (
          <IncidentMetricChart
            buckets={series}
            metric={incident.metric}
            baselineValue={incident.baseline_value}
            anomalyStart={anomalyStart}
          />
        ) : (
          <p className="text-xs text-text-3">
            No metric-series evidence is attached to this incident.
          </p>
        )}
      </SectionCard>

      {/* segment breakdown + diagnosis side by side on wide screens */}
      <div className="grid gap-8 min-[1440px]:grid-cols-2">
        <SectionCard
          title="Segment breakdown"
          description="Which methods, banks and gateways contributed to the deviation — flagged segments deviated by at least half the global deviation."
        >
          <IncidentSegmentBreakdown dimensions={segments} metric={incident.metric} />
        </SectionCard>

        <SectionCard
          title="Diagnosis"
          description="Most likely cause from the diagnosis model, with its ranked alternatives."
        >
          <IncidentDiagnosisCard diagnosis={incident.diagnosis} />
        </SectionCard>
      </div>

      {/* failure outliers + merchant-vs-network callout */}
      <SectionCard
        title="Failure outliers"
        description="Failure facets overrepresented in the incident window vs the pre-incident baseline, ranked by lift with min-count floors. The banner benchmarks the top facet against the whole platform stream (simulated fleet in this deployment)."
      >
        <IncidentInsightsPanel insights={extractInsights(incident)} />
      </SectionCard>

      {/* AI investigation */}
      <InvestigationPanel incidentId={incident.id} />

      {/* audit timeline */}
      <SectionCard
        title="Audit timeline"
        description="Every recorded event for this incident — detection, evidence, diagnoses, status changes and recovery actions."
      >
        <IncidentAuditTimeline events={incident.timeline} />
      </SectionCard>
    </div>
  );
}

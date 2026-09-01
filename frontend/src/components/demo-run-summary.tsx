"use client";

import * as React from "react";
import Link from "next/link";

import type { DemoResetResponse, ScenarioTriggerResponse } from "@/lib/types";
import { formatINR, formatNumber, timeAgo } from "@/lib/format";
import { StatusPill } from "@/components/status-pill";

/* ScenarioTriggerResponse.stats / .detection are Record<string, unknown> in the
   contract — narrow defensively so the panel only ever renders real values. */

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

interface DetectionIncident {
  incident_id?: unknown;
  action?: unknown;
  metric?: unknown;
  severity?: unknown;
  deviation_pct?: unknown;
  revenue_at_risk_paise?: unknown;
}

function Kv({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="bg-surface px-3 py-2.5">
      <dt className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">{label}</dt>
      <dd className="mt-0.5 truncate font-mono text-xs tabular-nums text-text">{children}</dd>
    </div>
  );
}

/** Result of POST /api/v1/demo/scenario/{name} — the real run summary. */
export function DemoRunSummary({ run }: { run: ScenarioTriggerResponse }) {
  const stats = asRecord(run.stats);
  const rows = asRecord(stats?.rows);
  const payments = asNumber(rows?.payments);
  const events = asNumber(rows?.payment_events);
  const captured = asNumber(stats?.captured_amount_paise);
  const failed = asNumber(stats?.failed_amount_paise);

  const detection = asRecord(run.detection);
  const anomalies = asNumber(detection?.anomalies_detected);
  const incidentsCreated = asNumber(detection?.incidents_created);
  const incidentsUpdated = asNumber(detection?.incidents_updated);
  const detectionIncidents = (
    Array.isArray(detection?.incidents) ? detection.incidents : []
  ) as DetectionIncident[];

  return (
    <div
      aria-live="polite"
      className="space-y-3 rounded-lg border border-border bg-raised/40 p-3.5"
    >
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill status={run.skipped ? "skipped" : run.status} />
        <p className="font-mono text-xs text-text">
          scenario <span className="text-accent">{run.scenario}</span>
        </p>
        {run.skipped ? (
          <span className="font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
            idempotent — identical run already seeded
          </span>
        ) : null}
      </div>
      {run.detail ? <p className="text-xs text-text-2">{run.detail}</p> : null}

      <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border md:grid-cols-3">
        <Kv label="simulator run">
          <span title={run.simulator_run_id ?? undefined}>{run.simulator_run_id ?? "—"}</span>
        </Kv>
        <Kv label="payments seeded">{payments === null ? "—" : formatNumber(payments)}</Kv>
        <Kv label="events seeded">{events === null ? "—" : formatNumber(events)}</Kv>
        <Kv label="captured volume">
          {captured === null ? "—" : formatINR(captured, { compact: true })}
        </Kv>
        <Kv label="failed volume">
          {failed === null ? "—" : formatINR(failed, { compact: true })}
        </Kv>
        <Kv label="detection pass">
          {anomalies === null
            ? "—"
            : `${formatNumber(anomalies)} anomal${anomalies === 1 ? "y" : "ies"} · ${
                incidentsCreated ?? 0
              } new · ${incidentsUpdated ?? 0} updated`}
        </Kv>
      </dl>

      {detectionIncidents.length > 0 ? (
        <ul className="space-y-1.5" aria-label="Detected incidents">
          {detectionIncidents.map((inc, i) => {
            const id = typeof inc.incident_id === "string" ? inc.incident_id : null;
            const severity = typeof inc.severity === "string" ? inc.severity : "UNKNOWN";
            const deviation = asNumber(inc.deviation_pct);
            const risk = asNumber(inc.revenue_at_risk_paise);
            return (
              <li
                key={id ?? i}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"
              >
                <StatusPill status={severity} />
                <span className="font-mono text-text-2">
                  {typeof inc.metric === "string" ? inc.metric : "metric"}
                  {deviation !== null
                    ? ` · ${deviation > 0 ? "+" : deviation < 0 ? "−" : ""}${Math.abs(deviation).toFixed(1)}%`
                    : ""}
                  {risk !== null ? ` · ${formatINR(risk, { compact: true })} at risk` : ""}
                </span>
                {id ? (
                  <Link
                    href={`/incidents/${id}`}
                    className="font-mono text-[11px] text-accent hover:text-accent-hover"
                  >
                    open incident →
                  </Link>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : run.incident_id ? (
        <Link
          href={`/incidents/${run.incident_id}`}
          className="inline-block font-mono text-[11px] text-accent hover:text-accent-hover"
        >
          open incident {run.incident_id} →
        </Link>
      ) : null}
    </div>
  );
}

/** Result of POST /api/v1/demo/reset — tables cleared, tables kept. */
export function DemoResetSummary({ result }: { result: DemoResetResponse }) {
  const clearedEntries = Object.entries(result.cleared ?? {});
  const totalCleared = clearedEntries.reduce((acc, [, n]) => acc + (typeof n === "number" ? n : 0), 0);
  const nonZero = clearedEntries.filter(([, n]) => typeof n === "number" && n > 0);

  return (
    <div
      aria-live="polite"
      className="space-y-3 rounded-lg border border-border bg-raised/40 p-3.5"
    >
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill status={result.status} />
        <p className="font-mono text-xs text-text">
          research dataset reset
          {result.reset_at ? <span className="text-text-3"> · {timeAgo(result.reset_at)}</span> : null}
        </p>
      </div>
      <p className="text-xs text-text-2">
        {formatNumber(totalCleared)} rows cleared
        {nonZero.length > 0
          ? ` — ${nonZero
              .map(([table, n]) => `${table.replace(/_/g, " ")} ${formatNumber(n)}`)
              .join(" · ")}`
          : " — the research dataset was already empty"}
        .
      </p>
      {result.kept && result.kept.length > 0 ? (
        <p className="font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
          kept (scientific record): {result.kept.join(", ")}
        </p>
      ) : null}
    </div>
  );
}

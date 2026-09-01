"use client";

import * as React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  CircleCheck,
  KeyRound,
  Loader2,
  PlugZap,
  RefreshCw,
  ShieldCheck,
  TriangleAlert,
  Unplug,
} from "lucide-react";

import { ApiError, api } from "@/lib/api";
import type { MerchantSyncResponse } from "@/lib/types";
import { formatDateTime, formatNumber, timeAgo } from "@/lib/format";
import { useMerchantConnection } from "@/components/merchant-connection";
import { EmptyState } from "@/components/empty-state";
import { ErrorPanel } from "@/components/error-panel";
import { PageHeader } from "@/components/page-header";
import { SectionCard } from "@/components/section-card";
import { StatusPill } from "@/components/status-pill";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ExportPanel } from "@/components/export/export-panel";
import { useEnvironment } from "@/components/environment-provider";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 py-2.5">
      <dt className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">{label}</dt>
      <dd className="flex items-center gap-2 text-[13px] text-text">{children}</dd>
    </div>
  );
}

/** entity_counts entries the sync run summary can carry — rendered only when present. */
const SYNC_ENTITIES: readonly [string, string][] = [
  ["orders", "orders"],
  ["payments", "payments"],
  ["payment_links", "payment links"],
  ["subscriptions", "subscriptions"],
];

/** Narrow one entity_counts bucket: {created, updated} or {fetched} or a bare number. */
function entityCountText(value: unknown): string | null {
  if (typeof value === "number" && Number.isFinite(value)) return formatNumber(value);
  if (typeof value !== "object" || value === null) return null;
  const rec = value as Record<string, unknown>;
  const parts: string[] = [];
  for (const key of ["created", "updated", "fetched"] as const) {
    const n = rec[key];
    if (typeof n === "number" && Number.isFinite(n)) parts.push(`${formatNumber(n)} ${key}`);
  }
  return parts.length > 0 ? parts.join(" · ") : null;
}

/** Quarantine entries arrive as {entity, id, reason} dicts from the sync
 * service — render them readably instead of "[object Object]". */
function quarantineText(entry: unknown): string {
  if (typeof entry === "string") return entry;
  if (typeof entry === "object" && entry !== null) {
    const e = entry as { entity?: unknown; id?: unknown; reason?: unknown };
    const head = [e.entity, e.id]
      .filter((p): p is string => typeof p === "string" && p.length > 0)
      .join(" ");
    const text = [head, typeof e.reason === "string" ? e.reason : ""]
      .filter(Boolean)
      .join(" — ");
    return text || JSON.stringify(entry);
  }
  return String(entry);
}

/** Real POST /merchant/sync run summary — per-entity counts + quarantine errors. */
function SyncRunSummary({ result }: { result: MerchantSyncResponse }) {
  const counts = result.entity_counts ?? {};
  const entityRows = SYNC_ENTITIES.map(([key, label]) => ({
    key,
    label,
    text: entityCountText(counts[key]),
  }));
  const quarantine = Array.isArray(counts.errors) ? counts.errors : [];

  return (
    <div
      role="status"
      aria-live="polite"
      className="space-y-3 rounded-lg border border-border bg-raised/40 p-3.5"
    >
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill status={result.status} />
        <p className="font-mono text-xs text-text">
          sync run <span className="text-text-3">{result.id}</span>
          {result.finished_at ? (
            <span className="text-text-3"> · {timeAgo(result.finished_at)}</span>
          ) : null}
        </p>
      </div>
      {result.error ? (
        <p className="flex items-start gap-2 text-xs text-text-2">
          <TriangleAlert aria-hidden className="mt-0.5 size-3.5 shrink-0 text-danger" />
          <span className="text-danger">{result.error}</span>
        </p>
      ) : null}
      <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border md:grid-cols-4">
        {entityRows.map(({ key, label, text }) => (
          <div key={key} className="bg-surface px-3 py-2.5">
            <dt className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
              {label}
            </dt>
            <dd className="mt-0.5 font-mono text-xs tabular-nums text-text">{text ?? "—"}</dd>
          </div>
        ))}
      </dl>
      {quarantine.length > 0 ? (
        <div className="flex items-start gap-2 text-xs text-text-2">
          <TriangleAlert aria-hidden className="mt-0.5 size-3.5 shrink-0 text-accent" />
          <div>
            <p className="font-medium text-accent">
              {formatNumber(quarantine.length)} row{quarantine.length === 1 ? "" : "s"} quarantined
              (skipped, recorded)
            </p>
            <ul className="mt-1 list-inside list-disc text-text-3">
              {quarantine.slice(0, 5).map((err, i) => (
                <li key={i} className="truncate" title={quarantineText(err)}>
                  {quarantineText(err)}
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : (
        <p className="text-xs text-text-3">No sync errors reported.</p>
      )}
    </div>
  );
}

/**
 * /settings — the Razorpay Test Mode connection surface. Every field comes
 * from GET /merchant/connection; the key secret is never rendered (the server
 * never sends it). The webhook probe deliberately sends a bad signature and
 * presents the expected 400 invalid_webhook_signature as positive proof that
 * verification is active.
 */
export function SettingsView() {
  const queryClient = useQueryClient();
  const { environment } = useEnvironment();
  const connection = useMerchantConnection();
  const [lastSync, setLastSync] = React.useState<MerchantSyncResponse | null>(null);

  const invalidateConnection = React.useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["merchant", "connection"] });
    void queryClient.invalidateQueries({ queryKey: ["payments"] });
    void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
  }, [queryClient]);

  const sync = useMutation({
    mutationFn: () => api.merchant.sync(),
    onSuccess: (data) => {
      setLastSync(data);
      invalidateConnection();
    },
  });

  const webhookProbe = useMutation({
    mutationFn: () =>
      api.webhooks.razorpay(
        { event: "console.probe", payload: { probe: true } },
        "console-webhook-probe",
      ),
  });

  const toggle = useMutation({
    mutationFn: (enable: boolean) =>
      enable ? api.merchant.enable() : api.merchant.disable(),
    onSuccess: () => invalidateConnection(),
  });

  const c = connection.data;
  const probeRejected =
    webhookProbe.error instanceof ApiError &&
    webhookProbe.error.status === 400 &&
    webhookProbe.error.code === "invalid_webhook_signature";

  return (
    <div className="space-y-6">
      <PageHeader
        title="Settings"
        description="Your Razorpay Test Mode connection — credentials stay on the server; this console only ever shows the masked key id."
      />

      <SectionCard
        title="Razorpay connection"
        description="Live state from the backend's merchant connection API"
        actions={
          c?.environment ? (
            <Badge variant="outline" title="Gateway environment reported by the server">
              {c.environment === "test" ? "test mode" : `${c.environment} mode`}
            </Badge>
          ) : undefined
        }
      >
        {connection.isPending ? (
          <div aria-busy="true" aria-label="Loading connection state" className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </div>
        ) : connection.isError || !c ? (
          <ErrorPanel
            error={connection.error}
            onRetry={() => connection.refetch()}
            title="Connection state unavailable"
          />
        ) : (
          <div className="space-y-4">
            <dl className="divide-y divide-border">
              <Row label="Environment">
                <span className="font-mono text-xs">
                  {c.environment === "live"
                    ? "Razorpay LIVE (check your keys!)"
                    : "Razorpay Test Mode"}
                </span>
              </Row>
              <Row label="Connection">
                <StatusPill
                  status={c.connected ? "live" : "down"}
                  dot
                />
                <span className="text-xs text-text-2">
                  {c.connected ? "Connected" : "Not connected"}
                </span>
                {c.connection_error && !c.connected ? (
                  <span
                    className="font-mono text-[10px] uppercase tracking-[0.07em] text-text-3"
                    title="Probe outcome reported by the server"
                  >
                    {c.connection_error.replace(/_/g, " ")}
                  </span>
                ) : null}
              </Row>
              <Row label="Key ID">
                <span className="font-mono text-xs text-text-2">
                  {c.key_id_masked ?? (c.configured ? "configured" : "—")}
                </span>
              </Row>
              <Row label="Key secret">
                <span className="font-mono text-xs tracking-[0.2em] text-text-3">••••••••</span>
                <span className="font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
                  never leaves the server
                </span>
              </Row>
              <Row label="Webhook">
                <StatusPill status={c.webhook_configured ? "ok" : "degraded"} />
                <span className="text-xs text-text-2">
                  {c.webhook_configured ? "Configured" : "Not configured"}
                </span>
              </Row>
              <Row label="Auto sync">
                <span className="text-xs text-text-2">{c.sync_enabled ? "Enabled" : "Disabled"}</span>
              </Row>
              <Row label="Last sync">
                <span className="font-mono text-xs tabular-nums text-text-2" title={c.last_sync_at ?? undefined}>
                  {c.last_sync_at ? formatDateTime(c.last_sync_at) : "never"}
                </span>
                {c.last_sync_status ? (
                  <span className="font-mono text-[10px] uppercase tracking-[0.07em] text-text-3">
                    {c.last_sync_status}
                  </span>
                ) : null}
              </Row>
              <Row label="Last webhook">
                <span className="font-mono text-xs tabular-nums text-text-2" title={c.last_webhook_at ?? undefined}>
                  {c.last_webhook_at ? formatDateTime(c.last_webhook_at) : "none received"}
                </span>
              </Row>
            </dl>

            {/* Actions */}
            <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
              <Button
                size="sm"
                disabled={sync.isPending || !c.connected}
                title={c.connected ? "Pull the latest rows from Razorpay Test Mode" : "Connect Razorpay Test Mode first"}
                onClick={() => sync.mutate()}
              >
                {sync.isPending ? <Loader2 aria-hidden className="animate-spin" /> : <RefreshCw aria-hidden />}
                {sync.isPending ? "Syncing" : "Sync now"}
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={webhookProbe.isPending}
                title="Send a probe with a deliberately invalid signature — a healthy endpoint rejects it"
                onClick={() => webhookProbe.mutate()}
              >
                {webhookProbe.isPending ? (
                  <Loader2 aria-hidden className="animate-spin" />
                ) : (
                  <ShieldCheck aria-hidden />
                )}
                {webhookProbe.isPending ? "Probing" : "Test webhook"}
              </Button>
              {c.sync_enabled ? (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={toggle.isPending}
                  onClick={() => toggle.mutate(false)}
                >
                  <Unplug aria-hidden />
                  Disconnect
                </Button>
              ) : (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={toggle.isPending || !c.configured}
                  title={c.configured ? "Enable syncing from the configured account" : "Configure credentials on the server first"}
                  onClick={() => toggle.mutate(true)}
                >
                  <PlugZap aria-hidden />
                  Connect
                </Button>
              )}
            </div>

            {/* Sync outcome — the real run summary */}
            {sync.isError ? (
              <ErrorPanel
                error={sync.error}
                onRetry={() => sync.mutate()}
                title="Sync failed"
              />
            ) : null}
            {lastSync && !sync.isPending ? <SyncRunSummary result={lastSync} /> : null}

            {/* Webhook probe outcome — rejection is the success case */}
            {webhookProbe.isSuccess ? (
              <div
                role="alert"
                className="flex items-start gap-3 rounded-lg border border-accent-border bg-accent-wash px-4 py-3.5"
              >
                <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-accent" />
                <p className="text-[13px] text-text-2">
                  <span className="font-medium text-text">Probe accepted.</span> The endpoint
                  did NOT reject an invalid signature — check RAZORPAY_WEBHOOK_SECRET on the
                  server before trusting webhook traffic.
                </p>
              </div>
            ) : null}
            {webhookProbe.isError ? (
              probeRejected ? (
                <div
                  role="status"
                  className="flex items-start gap-3 rounded-lg border border-[rgba(111,191,140,0.4)] bg-success-dim px-4 py-3.5"
                >
                  <CircleCheck aria-hidden className="mt-0.5 size-4 shrink-0 text-success" />
                  <p className="text-[13px] text-text-2">
                    <span className="font-medium text-text">Signature verification active.</span>{" "}
                    The endpoint rejected the probe&apos;s invalid signature (
                    <span className="font-mono text-xs">HTTP 400 invalid_webhook_signature</span>
                    ) — forged webhooks cannot get in.
                  </p>
                </div>
              ) : (
                <ErrorPanel
                  error={webhookProbe.error}
                  onRetry={() => webhookProbe.mutate()}
                  title="Webhook probe failed unexpectedly"
                />
              )
            ) : null}

            {toggle.isError ? (
              <ErrorPanel
                error={toggle.error}
                onRetry={() => toggle.mutate(!c.sync_enabled)}
                title="Could not change the connection state"
              />
            ) : null}
          </div>
        )}
      </SectionCard>

      <SectionCard
        title="How to connect"
        description="One-time server setup — credentials are read by the backend at startup and are never sent to this console"
      >
        <ol className="list-decimal space-y-2.5 pl-5 text-[13px] text-text-2">
          <li>
            Create <span className="font-medium text-text">test-mode API keys</span> in the
            Razorpay dashboard (Settings → API Keys → Generate Test Key).
          </li>
          <li>
            Set them in the backend <span className="font-mono text-xs">.env</span> file:
            <pre className="mt-2 overflow-auto rounded-md border border-border bg-bg p-3 font-mono text-[12px] leading-relaxed text-text-2">
{`RAZORPAY_KEY_ID=rzp_test_…
RAZORPAY_KEY_SECRET=…
RAZORPAY_WEBHOOK_SECRET=…
SIMULATION_MODE=false`}
            </pre>
          </li>
          <li>
            Restart the backend, then press{" "}
            <span className="font-medium text-text">Sync now</span> — observed orders, payments,
            payment links and subscriptions appear across the console.
          </li>
        </ol>
        <p className="mt-4 flex items-start gap-2 border-t border-border pt-3.5 text-xs text-text-3">
          <KeyRound aria-hidden className="mt-0.5 size-3.5 shrink-0" />
          Secrets never leave the server. The only credential-shaped value this console renders
          is the masked key id (e.g. rzp_test_••••1234), exactly as the server masks it.
        </p>
      </SectionCard>

      <ExportPanel environment={environment} />

      {!connection.isPending && (connection.isError || !c) ? (
        <EmptyState
          title="Merchant connection API not reachable"
          description="The settings surface needs GET /api/v1/merchant/connection. Once the backend serves it, connection state and actions appear here."
        />
      ) : null}
    </div>
  );
}

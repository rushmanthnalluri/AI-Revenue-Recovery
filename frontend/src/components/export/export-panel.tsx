"use client";

import * as React from "react";
import { Download, Loader2 } from "lucide-react";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { ErrorPanel } from "@/components/error-panel";
import { SectionCard } from "@/components/section-card";

type ExportKind = "audit" | "incidents" | "recovery" | "payments" | "summary";
type ExportFormat = "csv" | "json";

const EXPORT_KINDS: { id: ExportKind; label: string; description: string }[] = [
  {
    id: "audit",
    label: "Audit Trail",
    description: "Append-only record of every decision, action, and system event",
  },
  {
    id: "incidents",
    label: "Incidents",
    description: "Detected degradations with revenue impact and timeline",
  },
  {
    id: "recovery",
    label: "Recovery",
    description: "Opportunities, strategies, actions, and verification outcomes",
  },
  {
    id: "payments",
    label: "Payments",
    description: "Commerce records with full provenance (source_type, external_id, ingested_at)",
  },
  {
    id: "summary",
    label: "Summary",
    description: "Consolidated counts, revenue, and recovery rates",
  },
];

export function ExportPanel({ environment }: { environment: "real_test" | "research" }) {
  const [kind, setKind] = React.useState<ExportKind>("audit");
  const [format, setFormat] = React.useState<ExportFormat>("csv");
  const [exporting, setExporting] = React.useState(false);
  const [error, setError] = React.useState<unknown | null>(null);

  const handleExport = async () => {
    setExporting(true);
    setError(null);
    try {
      const blob = await api.export[kind]({
        environment,
        format,
      });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const filename = `${kind}_${environment}_${new Date().toISOString().slice(0, 19).replace(/:/g, "-")}.${format}`;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      setError(err);
    } finally {
      setExporting(false);
    }
  };

  return (
    <SectionCard
      title="Export Data"
      description="Download CSV or JSON exports scoped to the active environment. All exports include full provenance."
      className="max-w-2xl"
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor="export-kind" className="block text-xs font-medium text-text-3 mb-1.5">
              Export Type
            </label>
            <Select
              id="export-kind"
              value={kind}
              onChange={(e) => setKind(e.target.value as ExportKind)}
            >
              {EXPORT_KINDS.map((k) => (
                <option key={k.id} value={k.id}>
                  {k.label} — {k.description}
                </option>
              ))}
            </Select>
          </div>

          <div>
            <label htmlFor="export-format" className="block text-xs font-medium text-text-3 mb-1.5">
              Format
            </label>
            <Select
              id="export-format"
              value={format}
              onChange={(e) => setFormat(e.target.value as ExportFormat)}
            >
              <option value="csv">CSV — spreadsheet compatible</option>
              <option value="json">JSON — structured, nested data</option>
            </Select>
          </div>
        </div>

        {error ? (
          <ErrorPanel error={error} onRetry={handleExport} title="Export failed" />
        ) : null}

        <div className="flex items-center gap-3 pt-2 border-t border-border">
          <Button
            onClick={handleExport}
            disabled={exporting}
            className="flex items-center gap-2"
          >
            {exporting ? (
              <>
                <Loader2 className="size-4 animate-spin" />
                Exporting…
              </>
            ) : (
              <>
                <Download className="size-4" />
                Download {format.toUpperCase()}
              </>
            )}
          </Button>
          <span className="text-xs text-text-3">
            Environment: <code className="font-mono text-text">{environment}</code>
          </span>
        </div>
      </div>
    </SectionCard>
  );
}
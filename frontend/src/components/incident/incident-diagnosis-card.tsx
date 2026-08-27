import * as React from "react";
import { BrainCircuit, Cpu } from "lucide-react";

import type { DiagnosisView } from "@/lib/types";
import { formatDateTime } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { ConfidenceBar } from "@/components/confidence-bar";
import { EmptyState } from "@/components/empty-state";

export interface DiagnosisAlternative {
  cause: string;
  probability: number;
}

/**
 * The diagnosis explanation embeds the classifier's top-3 as
 * "Top-3: <label> <p>, <label> <p>, <label> <p>." — parse it back into
 * structured alternatives. Returns [] when the format differs; never guesses.
 */
export function parseTop3(explanation: string | null | undefined): DiagnosisAlternative[] {
  if (!explanation) return [];
  const match = /Top-3:\s*([^.]*)[.]/.exec(explanation);
  const group = match?.[1];
  if (!group) return [];
  return group
    .split(",")
    .map((part): DiagnosisAlternative | null => {
      const m = /^\s*(.+?)\s+(\d*\.?\d+)\s*$/.exec(part);
      const cause = m?.[1];
      const rawProbability = m?.[2];
      if (!cause || !rawProbability) return null;
      const probability = Number.parseFloat(rawProbability);
      if (!Number.isFinite(probability)) return null;
      return { cause, probability };
    })
    .filter((a): a is DiagnosisAlternative => a !== null);
}

/** The heuristic fallback is identifiable by model name / explanation prefix. */
export function isHeuristicDiagnosis(diagnosis: DiagnosisView): boolean {
  return (
    diagnosis.model_name === "diagnosis-heuristic" ||
    (diagnosis.explanation ?? "").startsWith("[heuristic]")
  );
}

interface IncidentDiagnosisCardProps {
  diagnosis: DiagnosisView | null | undefined;
  loading?: boolean;
}

/**
 * Diagnosis panel: predicted cause with calibrated confidence, the ranked
 * alternatives behind it, and an honesty badge when the rule-based fallback
 * produced the diagnosis instead of the trained classifier.
 */
export function IncidentDiagnosisCard({ diagnosis }: IncidentDiagnosisCardProps) {
  if (!diagnosis) {
    return (
      <EmptyState
        icon={BrainCircuit}
        title="No diagnosis yet"
        description="A diagnosis is produced automatically on first view; it may still be running."
      />
    );
  }

  const heuristic = isHeuristicDiagnosis(diagnosis);
  const alternatives = parseTop3(diagnosis.explanation).filter(
    (a) => a.cause !== diagnosis.predicted_cause,
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-base font-semibold tracking-tight text-text">
            {diagnosis.predicted_cause.replace(/_/g, " ")}
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <Badge variant={heuristic ? "warning" : "info"}>
              {heuristic ? (
                <Cpu className="size-3" strokeWidth={1.5} aria-hidden />
              ) : (
                <BrainCircuit className="size-3" strokeWidth={1.5} aria-hidden />
              )}
              {heuristic ? "heuristic fallback" : "ML classifier"}
            </Badge>
            <span className="font-mono text-2xs tabular-nums text-text-3">
              {diagnosis.model_name}@{diagnosis.model_version}
            </span>
            <span className="font-mono text-2xs tabular-nums text-text-3">
              {formatDateTime(diagnosis.created_at)}
            </span>
          </div>
        </div>
        <div className="flex flex-col items-end gap-1">
          <span className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
            confidence
          </span>
          <ConfidenceBar value={diagnosis.confidence} />
        </div>
      </div>

      {alternatives.length > 0 ? (
        <div>
          <h4 className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
            alternative causes
          </h4>
          <ul className="mt-2 space-y-1.5">
            {alternatives.map((alt, i) => (
              <li key={alt.cause} className="flex items-center justify-between gap-3">
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className="font-mono text-2xs tabular-nums text-text-3">#{i + 2}</span>
                  <span className="truncate text-[13px] text-text-2">
                    {alt.cause.replace(/_/g, " ")}
                  </span>
                </span>
                <ConfidenceBar value={alt.probability} className="shrink-0" />
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {diagnosis.explanation ? (
        <p className="border-t border-border pt-3 text-xs leading-relaxed text-text-3">
          {diagnosis.explanation}
        </p>
      ) : null}
    </div>
  );
}

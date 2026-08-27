"use client";

import * as React from "react";
import { Loader2, Play } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import type { ScenarioInfo } from "@/lib/types";

/** Backend presets (app/simulator/config.py SCENARIOS) — used only when the
    scenarios endpoint can't be reached; the POST will surface any error. */
const FALLBACK_SCENARIOS = ["standard", "upi_outage_demo"] as const;

interface EvaluationRunTriggerProps {
  scenarios: ScenarioInfo[] | undefined;
  /** True while the synchronous POST is in flight (up to the client timeout). */
  isPending: boolean;
  onRun: (scenario: string) => void;
}

/** Scenario picker + run trigger for the evaluation harness. */
export function EvaluationRunTrigger({ scenarios, isPending, onRun }: EvaluationRunTriggerProps) {
  const names = React.useMemo(
    () => (scenarios && scenarios.length > 0 ? scenarios.map((s) => s.name) : [...FALLBACK_SCENARIOS]),
    [scenarios],
  );
  const [scenario, setScenario] = React.useState<string>("standard");
  const effective = names.includes(scenario) ? scenario : (names[0] ?? "standard");

  return (
    <div className="flex items-center gap-2">
      <Select
        value={effective}
        onChange={(e) => setScenario(e.target.value)}
        aria-label="Simulator scenario for the evaluation run"
        title={
          scenarios?.find((s) => s.name === effective)?.description ?? "Simulator scenario preset"
        }
        className="h-7 w-auto max-w-[180px] px-2 text-xs"
        disabled={isPending}
      >
        {names.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </Select>
      <Button size="sm" onClick={() => onRun(effective)} disabled={isPending}>
        {isPending ? (
          <Loader2 className="size-3.5 animate-spin" aria-hidden />
        ) : (
          <Play className="size-3.5" aria-hidden />
        )}
        {isPending ? "Starting…" : "Run evaluation"}
      </Button>
    </div>
  );
}

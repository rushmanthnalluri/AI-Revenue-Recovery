import * as React from "react";

/**
 * Methodology & honest caveats — the lab notebook panel. These mirror
 * docs/evaluation.md §1/§3 verbatim in spirit: what the harness does, what
 * it discloses, and where the numbers are weak. Static documentation text,
 * never run data.
 */
const CAVEATS: { title: string; body: string }[] = [
  {
    title: "Two arms, same seed",
    body: "Each run executes the identical simulator scenario twice: BASELINE fires one ungated retry at every failed payment; PULSECOVER runs the real loop — detection passes, ML diagnosis, the deterministic policy gate, execution through the gateway port, and webhook verification.",
  },
  {
    title: "Shared conversion prior",
    body: "Whether a recovery attempt converts is decided by a documented (failure-class × action) conversion table, seeded per request and identical for both arms. The comparison is fair even where absolute numbers are prior-driven — the prior is a model, not a measurement.",
  },
  {
    title: "The harness plays two disclosed roles",
    body: "It is the operator (approving every REQUIRES_APPROVAL decision as human:eval_operator) and the customer (seeded conversion). Both are deterministic and counted — approvals_required is reported per run.",
  },
  {
    title: "Detection scores honestly",
    body: "Scheduled passes also cover quiet traffic, so false positives count against precision. route_latency incidents are near-invisible to global-metric detection — a real coverage gap the evaluation exposes, not a bug in the scorer.",
  },
  {
    title: "Isolated scratch databases",
    body: "Arms run in throwaway SQLite DBs; only the evaluation_runs and experiments rows persist here. Arm simulator_run_ids live inside the metrics JSON (arms.*.simulator_run_id). MTTD is simulator-time; MTTR is wall-clock pipeline latency.",
  },
  {
    title: "Synchronous execution",
    body: "POST /api/v1/evaluation/run blocks for the whole run — seconds at reduced scale, minutes at the full preset. This console treats the request as fire-and-poll: the stored run row is the source of truth.",
  },
];

export function EvaluationMethodology({ notes }: { notes?: string | null }) {
  return (
    <div>
      {notes ? (
        <div className="mb-4 rounded-md border border-border bg-bg px-3.5 py-3">
          <p className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
            Run notes (stored)
          </p>
          <p className="mt-1 font-mono text-xs leading-relaxed text-text-2">{notes}</p>
        </div>
      ) : null}

      <ul className="divide-y divide-border">
        {CAVEATS.map((c) => (
          <li key={c.title} className="py-2.5 first:pt-0 last:pb-0">
            <p className="text-[13px] font-medium text-text">{c.title}</p>
            <p className="mt-0.5 text-xs leading-relaxed text-text-3">{c.body}</p>
          </li>
        ))}
      </ul>

      <div className="mt-4 rounded-md border border-border bg-bg px-3.5 py-3">
        <p className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-3">
          Reproduce
        </p>
        <pre className="mt-1 overflow-x-auto font-mono text-[11px] leading-relaxed text-text-2">
{`cd backend
.venv/Scripts/python scripts/run_evaluation.py --scenario standard
# reduced-scale smoke run:
.venv/Scripts/python scripts/run_evaluation.py --scenario upi_outage_demo --days 5 --events 8000`}
        </pre>
      </div>
    </div>
  );
}

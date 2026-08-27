import { readFileSync } from "node:fs";
import path from "node:path";

/** What global-setup guarantees before any spec runs. */
export interface SeedState {
  /** Incident detected by the seeded scenario's anchored detection pass. */
  incidentId: string | null;
  /** Opportunity whose open action sits at PENDING_APPROVAL (policy-gated). */
  pendingOpportunityId: string | null;
  /** Name of a stored evaluation run (may be completed or failed). */
  evaluationRunName: string | null;
  seededAt: string;
}

export const SEED_STATE_PATH = path.resolve(__dirname, ".tmp", "seed-state.json");

export function readSeedState(): SeedState {
  return JSON.parse(readFileSync(SEED_STATE_PATH, "utf-8")) as SeedState;
}

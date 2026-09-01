/**
 * Environment vocabulary — the single source of truth for how the two data
 * environments are named in user-visible copy.
 *
 * real_test  → the primary product: the merchant's REAL Razorpay Test Mode
 *              account. Switcher label "Real Merchant"; data provenance label
 *              "Razorpay Test Mode".
 * research   → the isolated Research Lab: synthetic simulator data only.
 *              Switcher label "Research Lab"; data provenance label
 *              "Synthetic Research Dataset".
 *
 * Label discipline: the bare words "simulation"/"demo" never appear
 * user-visible; "simulator" appears only inside Research Lab copy where the
 * harness is the subject.
 */

import type { Environment } from "@/lib/types";

export const DEFAULT_ENVIRONMENT: Environment = "real_test";

/** Two-segment switcher labels (rendered uppercase by the component). */
export const ENVIRONMENT_SWITCH_LABEL: Record<Environment, string> = {
  real_test: "Real Merchant",
  research: "Research Lab",
};

/** Short environment badges (audit rows, pipeline rows). */
export const ENVIRONMENT_BADGE_LABEL: Record<Environment, string> = {
  real_test: "Real Test",
  research: "Research",
};

/** Provenance chip prefix next to data surfaces. */
export function environmentDataLabel(environment: Environment): string {
  return environment === "real_test" ? "Razorpay Test Mode" : "Synthetic Research Dataset";
}

/** Payment `source_type` → honest source badge label. */
export function sourceTypeLabel(sourceType: string): string {
  switch (sourceType) {
    case "razorpay_test":
      return "razorpay test";
    case "razorpay_live":
      return "razorpay live";
    case "simulator":
      return "research";
    default:
      return sourceType;
  }
}

/** The environment a commerce `source_type` belongs to (mirror of the backend mapping). */
export function environmentForSourceType(sourceType: string): Environment {
  return sourceType === "simulator" ? "research" : "real_test";
}

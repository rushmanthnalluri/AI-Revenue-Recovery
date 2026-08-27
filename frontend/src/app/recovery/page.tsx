import type { Metadata } from "next";

import { RecoveryPlannerView } from "@/components/recovery/recovery-view";

export const metadata: Metadata = {
  title: "Recovery",
};

export default function RecoveryPage() {
  return <RecoveryPlannerView />;
}

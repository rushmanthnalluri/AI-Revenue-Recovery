import type { Metadata } from "next";
import * as React from "react";

import { RecoveryPlannerView } from "@/components/recovery/recovery-view";

export const metadata: Metadata = {
  title: "Recovery",
};

export default function RecoveryPage() {
  return (
    <React.Suspense fallback={null}>
      <RecoveryPlannerView />
    </React.Suspense>
  );
}

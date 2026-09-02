import type { Metadata } from "next";
import * as React from "react";

import { AuditView } from "@/components/audit/audit-view";

export const metadata: Metadata = {
  title: "Audit Trail",
};

export default function AuditPage() {
  return (
    <React.Suspense fallback={null}>
      <AuditView />
    </React.Suspense>
  );
}

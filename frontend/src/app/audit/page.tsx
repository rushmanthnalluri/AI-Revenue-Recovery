import type { Metadata } from "next";

import { AuditView } from "@/components/audit/audit-view";

export const metadata: Metadata = {
  title: "Audit Trail",
};

export default function AuditPage() {
  return <AuditView />;
}

import type { Metadata } from "next";

import { IncidentDetailView } from "@/components/incident/incident-detail-view";

export const metadata: Metadata = {
  title: "Incident intelligence",
};

export default async function IncidentDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <IncidentDetailView incidentId={id} />;
}

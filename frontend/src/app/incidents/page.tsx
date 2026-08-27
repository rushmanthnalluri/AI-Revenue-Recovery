import type { Metadata } from "next";

import { IncidentListView } from "@/components/incident/incident-list-view";

export const metadata: Metadata = {
  title: "Incidents",
};

export default function IncidentsPage() {
  return <IncidentListView />;
}

import type { Metadata } from "next";
import { Suspense } from "react";

import { ResearchView } from "@/components/research/research-view";
import { Skeleton } from "@/components/ui/skeleton";

export const metadata: Metadata = {
  title: "Research Lab",
};

/** Suspense boundary required by useSearchParams (tab + run deep links). */
export default function ResearchPage() {
  return (
    <Suspense
      fallback={
        <div className="space-y-6" aria-busy="true" aria-label="Loading research lab">
          <Skeleton className="h-14 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      }
    >
      <ResearchView />
    </Suspense>
  );
}

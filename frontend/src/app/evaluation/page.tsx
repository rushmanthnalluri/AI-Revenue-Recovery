import type { Metadata } from "next";
import { Suspense } from "react";

import { EvaluationView } from "@/components/evaluation/evaluation-view";
import { Skeleton } from "@/components/ui/skeleton";

export const metadata: Metadata = {
  title: "Evaluation Lab",
};

/** Suspense boundary required by useSearchParams (run selection deep links). */
export default function EvaluationPage() {
  return (
    <Suspense
      fallback={
        <div className="space-y-6" aria-busy="true" aria-label="Loading evaluation lab">
          <Skeleton className="h-14 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      }
    >
      <EvaluationView />
    </Suspense>
  );
}

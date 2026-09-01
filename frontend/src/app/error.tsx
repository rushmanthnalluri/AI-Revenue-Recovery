"use client";

import { Button } from "@/components/ui/button";
import { TriangleAlert } from "lucide-react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="flex flex-col items-start gap-3 rounded-lg border border-danger/30 bg-danger/5 px-5 py-6">
      <div className="flex items-center gap-2 text-danger">
        <TriangleAlert className="size-4" aria-hidden />
        <p className="text-sm font-semibold">Something went wrong</p>
      </div>
      <p className="text-xs text-text-3">{error.message || "Unexpected UI error."}</p>
      <Button variant="outline" size="sm" onClick={reset}>
        Try again
      </Button>
    </div>
  );
}

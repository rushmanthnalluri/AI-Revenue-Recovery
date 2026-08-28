"use client";

import * as React from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Hammer, Loader2 } from "lucide-react";

import { api } from "@/lib/api";
import { isOpenIncidentStatus } from "@/lib/types";
import { ErrorPanel } from "@/components/error-panel";
import { Button, buttonVariants } from "@/components/ui/button";
import { useBuildOpportunities } from "@/components/recovery/recovery-hooks";

function CommandCenterLink() {
  return (
    <Link href="/" className={buttonVariants({ variant: "secondary", size: "sm" })}>
      Open the Command Center
    </Link>
  );
}

/**
 * Empty-pipeline guide. When no opportunities exist yet but an open
 * (non-terminal) incident does, offer the direct path: build opportunities
 * from the newest open incident — the incidents endpoint returns newest
 * first, so the first open row of the first page is the latest one. On
 * success the shared hook invalidates the pipeline query, the table fills
 * in and this empty state unmounts. When there is no open incident (or the
 * lookup fails), fall back to the Command Center nudge.
 */
export function BuildFromIncidentAction() {
  const latestOpen = useQuery({
    queryKey: ["incidents", "latest-open"],
    queryFn: async () => {
      const res = await api.incidents.list({ page: 1, page_size: 50 });
      return res.items.find((item) => isOpenIncidentStatus(item.status)) ?? null;
    },
    staleTime: 30_000,
  });
  const mutation = useBuildOpportunities();

  // Don't flash the fallback CTA while the lookup is in flight.
  if (latestOpen.isPending) return null;
  const incident = latestOpen.data ?? null;
  if (!incident) return <CommandCenterLink />;

  const builtNothing =
    mutation.data !== undefined &&
    mutation.data.created_count + mutation.data.existing_count === 0;

  return (
    <div className="flex flex-col items-center gap-2">
      <p className="max-w-md text-xs text-text-3">
        Open incident{" "}
        <Link
          href={`/incidents/${incident.id}`}
          className="font-medium text-text-2 transition-colors duration-150 ease-apple hover:text-accent"
        >
          {incident.title}
        </Link>{" "}
        has no recovery opportunities yet.
      </p>
      <Button
        size="sm"
        disabled={mutation.isPending}
        onClick={() => mutation.mutate(incident.id)}
      >
        {mutation.isPending ? (
          <Loader2 className="size-4 animate-spin" strokeWidth={1.5} aria-hidden />
        ) : (
          <Hammer className="size-4" strokeWidth={1.5} aria-hidden />
        )}
        {mutation.isPending ? "Building…" : "Build from latest incident"}
      </Button>
      {mutation.isError ? (
        <ErrorPanel
          error={mutation.error}
          title="Could not build opportunities"
          onRetry={() => mutation.mutate(incident.id)}
          className="mt-1 w-full max-w-md"
        />
      ) : null}
      {builtNothing ? (
        <p role="status" className="max-w-md text-xs text-text-3">
          Nothing to build — this incident&apos;s window has no failed payments or dropped
          checkouts. Trigger a fresh scenario instead.
        </p>
      ) : null}
    </div>
  );
}

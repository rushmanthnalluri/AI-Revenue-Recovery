"use client";

import * as React from "react";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { PageReveal } from "@/components/page-reveal";
import { Sidebar } from "@/components/sidebar";
import { Topbar } from "@/components/topbar";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  /* Single owner of the system-health poll (20s) — the result is handed to
     the sidebar-footer / topbar status pills so they never poll twice. */
  const health = useQuery({
    queryKey: ["system", "health"],
    queryFn: () => api.system.health(),
    refetchInterval: 20_000,
    retry: 0,
  });

  return (
    <div className="page-wash min-h-screen bg-bg">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-md focus:bg-accent focus:px-3 focus:py-2 focus:text-[13px] focus:font-medium focus:text-accent-ink"
      >
        Skip to content
      </a>
      <Sidebar health={health} />
      <div className="md:pl-[264px]">
        <Topbar health={health} />
        <main
          id="main-content"
          className="mx-auto w-full max-w-[940px] px-4 py-8 md:px-8 min-[1440px]:max-w-[1200px]"
        >
          <PageReveal key={pathname}>{children}</PageReveal>
        </main>
      </div>
    </div>
  );
}

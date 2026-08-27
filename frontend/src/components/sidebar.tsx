"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  FlaskConical,
  LayoutDashboard,
  RotateCcw,
  ScrollText,
  Siren,
} from "lucide-react";

import { LiveStatusPill, type HealthQuery } from "@/components/live-status-pill";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { href: "/", label: "Command Center", icon: LayoutDashboard },
  { href: "/incidents", label: "Incidents", icon: Siren },
  { href: "/recovery", label: "Recovery", icon: RotateCcw },
  { href: "/audit", label: "Audit Trail", icon: ScrollText },
  { href: "/evaluation", label: "Evaluation Lab", icon: FlaskConical },
] as const;

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * 264px sticky sidebar — spec: bg-surface one step above the page backdrop,
 * right hairline; amber-gradient 28px brand tile with mono mark; mono section
 * captions; active item = accent-wash gradient + inset 2px amber bar (the
 * .nav-item-active component class); footer carries the live API status pill.
 */
export function Sidebar({ health }: { health: HealthQuery }) {
  const pathname = usePathname();

  return (
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-[264px] flex-col border-r border-border bg-surface md:flex">
      {/* Brand row */}
      <div className="flex items-center gap-2.5 border-b border-border px-4 py-4">
        <span
          aria-hidden
          className="flex size-7 items-center justify-center rounded-md bg-[linear-gradient(155deg,#E3B254,#D9A63F_55%,#B8892E)] shadow-[inset_0_1px_0_rgba(255,255,255,0.25)]"
        >
          <span className="font-mono text-[11px] font-semibold tracking-tight text-accent-ink">
            PR
          </span>
        </span>
        <div className="leading-tight">
          <p className="text-[13.5px] font-semibold tracking-tight text-text">PulseRecover</p>
          <p className="font-mono text-[9.5px] uppercase tracking-[0.11em] text-text-3">
            Revenue Recovery Ops
          </p>
        </div>
      </div>

      {/* Primary nav */}
      <nav aria-label="Primary" className="flex-1 overflow-y-auto px-2.5 pb-2">
        <p className="px-2 pb-1.5 pt-4 font-mono text-[10px] uppercase tracking-[0.11em] text-text-3">
          Console
        </p>
        <div className="space-y-0.5">
          {NAV_ITEMS.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-[13px] transition-colors duration-150 ease-apple",
                  active ? "nav-item-active" : "text-text-2 hover:bg-raised hover:text-text",
                )}
              >
                <item.icon
                  className={cn("size-[18px]", active ? "text-accent" : "text-text-3")}
                  strokeWidth={1.5}
                  aria-hidden
                />
                {item.label}
              </Link>
            );
          })}
        </div>
      </nav>

      {/* Footer: live API status + mono tagline */}
      <div className="space-y-2.5 border-t border-border p-3">
        <LiveStatusPill health={health} className="w-full justify-center" />
        <p className="text-center font-mono text-[9.5px] uppercase tracking-[0.07em] text-text-3">
          AI proposes · Policy decides
        </p>
      </div>
    </aside>
  );
}

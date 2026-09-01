"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  FlaskConical,
  IndianRupee,
  LayoutDashboard,
  RotateCcw,
  ScrollText,
  Settings,
  Siren,
  type LucideIcon,
} from "lucide-react";

import { EnvironmentSwitcher } from "@/components/environment-switcher";
import { LiveStatusPill, type HealthQuery } from "@/components/live-status-pill";
import { cn } from "@/lib/utils";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

/**
 * Information architecture: the Console section is the merchant-facing
 * product (env-scoped data, default real_test); the Workspace section holds
 * the isolated Research Lab (synthetic data) and Settings.
 */
export const NAV_SECTIONS: readonly { caption: string; items: readonly NavItem[] }[] = [
  {
    caption: "Console",
    items: [
      { href: "/", label: "Command Center", icon: LayoutDashboard },
      { href: "/payments", label: "Payments", icon: IndianRupee },
      { href: "/incidents", label: "Incidents", icon: Siren },
      { href: "/recovery", label: "Recovery", icon: RotateCcw },
      { href: "/audit", label: "Audit Trail", icon: ScrollText },
    ],
  },
  {
    caption: "Workspace",
    items: [
      { href: "/research", label: "Research Lab", icon: FlaskConical },
      { href: "/settings", label: "Settings", icon: Settings },
    ],
  },
] as const;

export const NAV_ITEMS: readonly NavItem[] = NAV_SECTIONS.flatMap((section) => section.items);

export function isNavActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function NavItems({ pathname }: { pathname: string }) {
  return (
    <>
      {NAV_SECTIONS.map((section) => (
        <div key={section.caption}>
          <p className="px-2 pb-1.5 pt-4 font-mono text-[10px] uppercase tracking-[0.11em] text-text-3">
            {section.caption}
          </p>
          <div className="space-y-0.5">
            {section.items.map((item) => {
              const active = isNavActive(pathname, item.href);
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
        </div>
      ))}
    </>
  );
}

/**
 * 264px sticky sidebar — spec: bg-surface one step above the page backdrop,
 * right hairline; amber-gradient 28px brand tile with mono mark; mono section
 * captions; active item = accent-wash gradient + inset 2px amber bar (the
 * .nav-item-active component class); footer carries the live API status pill.
 * The environment switcher sits directly under the brand row — the data
 * boundary is part of the chrome, not a page-level control.
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

      {/* Environment switcher — REAL MERCHANT / RESEARCH LAB */}
      <div className="border-b border-border px-2.5 pb-3 pt-3">
        <EnvironmentSwitcher />
      </div>

      {/* Primary nav */}
      <nav aria-label="Primary" className="flex-1 overflow-y-auto px-2.5 pb-2">
        <NavItems pathname={pathname} />
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

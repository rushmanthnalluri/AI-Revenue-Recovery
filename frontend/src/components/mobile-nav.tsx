"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { usePathname } from "next/navigation";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Menu, X } from "lucide-react";

import { EnvironmentSwitcher } from "@/components/environment-switcher";
import { NavItems } from "@/components/sidebar";
import { useModalA11y } from "@/components/recovery/recovery-hooks";

/**
 * Mobile navigation (below md, where the sidebar is hidden): a hamburger in
 * the topbar opens a left slide-over carrying the same primary nav. Mirrors
 * the opportunity drawer's floating-layer recipe — bg-elevated, strong
 * hairline, float shadow — and its a11y: focus trap, Escape to close, body
 * scroll lock, focus restore (useModalA11y). The layer is portaled to <body>:
 * the topbar's backdrop-blur would otherwise become its containing block and
 * clip the fixed overlay to the 56px header.
 */
export function MobileNav() {
  const pathname = usePathname();
  const [open, setOpen] = React.useState(false);
  const [mounted, setMounted] = React.useState(false);
  const reduceMotion = useReducedMotion();
  const panelRef = React.useRef<HTMLDivElement | null>(null);

  const close = React.useCallback(() => setOpen(false), []);
  useModalA11y(panelRef, close, open);

  // Portals need the DOM — client-only after mount.
  React.useEffect(() => setMounted(true), []);

  // Route change always closes the drawer.
  React.useEffect(() => {
    setOpen(false);
  }, [pathname]);

  return (
    <>
      <button
        type="button"
        aria-label="Open navigation"
        aria-expanded={open}
        aria-controls="mobile-nav-drawer"
        onClick={() => setOpen(true)}
        className="flex size-8 items-center justify-center rounded-md border border-border-strong text-text-2 transition-colors duration-150 ease-apple hover:bg-surface hover:text-text md:hidden"
      >
        <Menu className="size-[18px]" strokeWidth={1.5} aria-hidden />
      </button>

      {mounted
        ? createPortal(
            <AnimatePresence>
              {open ? (
                <motion.div className="fixed inset-0 z-50 md:hidden">
            <motion.div
              aria-hidden
              className="absolute inset-0 bg-black/60"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: reduceMotion ? 0 : 0.15 }}
              onClick={close}
            />
            <motion.div
              ref={panelRef}
              id="mobile-nav-drawer"
              role="dialog"
              aria-modal="true"
              aria-label="Primary navigation"
              tabIndex={-1}
              className="absolute inset-y-0 left-0 flex w-[264px] flex-col border-r border-border-strong bg-elevated shadow-float"
              initial={{ x: reduceMotion ? 0 : -264 }}
              animate={{ x: 0 }}
              exit={{ x: reduceMotion ? 0 : -264 }}
              transition={{ duration: reduceMotion ? 0 : 0.25, ease: [0.32, 0.72, 0, 1] }}
            >
              <div className="flex items-center justify-between border-b border-border px-4 py-4">
                <div className="flex items-center gap-2.5">
                  <span
                    aria-hidden
                    className="flex size-7 items-center justify-center rounded-md bg-[linear-gradient(155deg,#E3B254,#D9A63F_55%,#B8892E)] shadow-[inset_0_1px_0_rgba(255,255,255,0.25)]"
                  >
                    <span className="font-mono text-[11px] font-semibold tracking-tight text-accent-ink">
                      PR
                    </span>
                  </span>
                  <p className="text-[13.5px] font-semibold tracking-tight text-text">
                    PulseRecover
                  </p>
                </div>
                <button
                  type="button"
                  aria-label="Close navigation"
                  onClick={close}
                  className="flex size-8 items-center justify-center rounded-md text-text-2 transition-colors duration-150 ease-apple hover:bg-surface hover:text-text"
                >
                  <X className="size-[18px]" strokeWidth={1.5} aria-hidden />
                </button>
              </div>

              {/* Environment switcher — same control as the sidebar */}
              <div className="border-b border-border px-2.5 pb-3 pt-3">
                <EnvironmentSwitcher />
              </div>

              <nav aria-label="Mobile" className="flex-1 overflow-y-auto px-2.5 pb-2">
                <NavItems pathname={pathname} />
              </nav>
            </motion.div>
                </motion.div>
              ) : null}
            </AnimatePresence>,
            document.body,
          )
        : null}
    </>
  );
}

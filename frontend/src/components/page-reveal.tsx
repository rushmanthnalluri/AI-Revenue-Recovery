"use client";

import * as React from "react";
import { motion, useReducedMotion } from "framer-motion";

/**
 * Route reveal — the only framer-motion use sanctioned by the design spec:
 * fade + 10px rise, ≤450ms, on the apple ease curve. Renders statically when
 * the user prefers reduced motion. Keyed by pathname in AppShell so every
 * route change replays it once.
 */
export function PageReveal({ children }: { children: React.ReactNode }) {
  const reduce = useReducedMotion();

  if (reduce) {
    return <>{children}</>;
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: [0.32, 0.72, 0, 1] }}
    >
      {children}
    </motion.div>
  );
}

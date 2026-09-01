/**
 * Shared hooks for the Recovery Planner / Approval Center screens.
 */

"use client";

import * as React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

/** Mutations are always attributed to the console operator. */
export const CONSOLE_ACTOR = "human:console";

/**
 * Recovery mutations touch the opportunity lists, the detail/plan of the
 * affected opportunity and the dashboard KPIs (pending approvals, recovered
 * revenue) — invalidate all three so every surface refreshes from truth.
 */
export function useInvalidateRecovery() {
  const queryClient = useQueryClient();
  return React.useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["recovery"] });
    void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
  }, [queryClient]);
}

/**
 * Build an incident's recovery opportunities (POST /recovery/opportunities/
 * build). Idempotent server-side: payments/orders that already have an
 * opportunity are reused and reported under `existing_count`, so a re-run
 * never duplicates. On success the recovery surfaces plus the incident
 * queries (header counts, latest-open lookup) are invalidated so every
 * surface refreshes from truth.
 */
export function useBuildOpportunities() {
  const queryClient = useQueryClient();
  const invalidateRecovery = useInvalidateRecovery();
  return useMutation({
    mutationFn: (incidentId: string) =>
      api.recovery.buildOpportunities({ incident_id: incidentId, actor: CONSOLE_ACTOR }),
    onSuccess: () => {
      invalidateRecovery();
      void queryClient.invalidateQueries({ queryKey: ["incidents"] });
    },
  });
}

/**
 * Operator-triggered reconciliation sweep (POST /recovery/reconcile, ADR
 * 0011): resolves UNKNOWN actions against gateway truth (GETs only) and
 * reprocesses failed webhook events. Idempotent — safe to re-run. On success
 * the recovery surfaces, dashboard KPIs and the audit trail (the sweep
 * records its own `recovery.reconcile` audit row) are invalidated so every
 * surface refreshes from truth.
 */
export function useReconcile() {
  const queryClient = useQueryClient();
  const invalidateRecovery = useInvalidateRecovery();
  return useMutation({
    mutationFn: () => api.recovery.reconcile({ actor: CONSOLE_ACTOR }),
    onSuccess: () => {
      invalidateRecovery();
      void queryClient.invalidateQueries({ queryKey: ["audit"] });
    },
  });
}

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Minimal modal accessibility for the drawer's floating layers:
 * focuses the first control on open, traps Tab inside the layer, closes on
 * Escape, locks body scroll, and restores focus on unmount. Attach the ref to
 * the dialog/drawer panel element.
 *
 * `onClose` is read through a ref so an inline closure from the caller does
 * not re-install the trap on every render — re-running the effect would
 * re-fire the initial focus and steal it from whatever the operator is doing
 * (e.g. while a polling query re-renders the parent every few seconds).
 */
export function useModalA11y(
  ref: React.RefObject<HTMLElement | null>,
  onClose: () => void,
  active = true,
) {
  const onCloseRef = React.useRef(onClose);
  React.useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  React.useEffect(() => {
    if (!active) return;
    const node = ref.current;
    if (!node) return;

    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;

    const focusables = () =>
      Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );

    const initial = focusables()[0] ?? node;
    initial.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusables();
      if (items.length === 0) {
        event.preventDefault();
        node.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    node.addEventListener("keydown", onKeyDown);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    return () => {
      node.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previouslyFocused?.focus();
    };
  }, [ref, active]);
}

"use client";

import * as React from "react";

import { DEFAULT_ENVIRONMENT } from "@/lib/environment";
import type { Environment } from "@/lib/types";

const STORAGE_KEY = "pulserecover:environment";

interface EnvironmentContextValue {
  /** Active data environment — every scoped read threads this. Default real_test. */
  environment: Environment;
  setEnvironment: (environment: Environment) => void;
}

const EnvironmentContext = React.createContext<EnvironmentContextValue | null>(null);

/**
 * Owns the real_test/research selection for the whole console. Persisted to
 * localStorage so a refresh keeps the operator in the environment they were
 * inspecting; the default is always the real merchant surface.
 */
export function EnvironmentProvider({ children }: { children: React.ReactNode }) {
  const [environment, setEnvironmentState] = React.useState<Environment>(DEFAULT_ENVIRONMENT);

  // Hydrate after mount — server render must stay deterministic (real_test).
  React.useEffect(() => {
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      if (stored === "real_test" || stored === "research") setEnvironmentState(stored);
    } catch {
      /* storage unavailable — keep the default */
    }
  }, []);

  const setEnvironment = React.useCallback((next: Environment) => {
    setEnvironmentState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* storage unavailable — session-only selection */
    }
  }, []);

  const value = React.useMemo(
    () => ({ environment, setEnvironment }),
    [environment, setEnvironment],
  );

  return <EnvironmentContext.Provider value={value}>{children}</EnvironmentContext.Provider>;
}

export function useEnvironment(): EnvironmentContextValue {
  const ctx = React.useContext(EnvironmentContext);
  if (!ctx) throw new Error("useEnvironment must be used within <EnvironmentProvider>");
  return ctx;
}

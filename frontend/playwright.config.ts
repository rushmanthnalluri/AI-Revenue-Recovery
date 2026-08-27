import { defineConfig, devices } from "@playwright/test";

import { API_BASE_URL, API_KEY, BACKEND_PORT, FRONTEND_BASE_URL, FRONTEND_PORT, SCRATCH_DATABASE_URL } from "./e2e/stack";

/**
 * Playwright e2e for the PulseRecover console (chromium only).
 *
 * Two webServers, both reused when already up:
 *   1. scratch FastAPI on :8001 — own throwaway SQLite DB
 *      (backend/e2e_test.db), simulation mode, CORS opened to the console;
 *   2. Next.js dev server on :3100 — dev (not `next start`) so the
 *      NEXT_PUBLIC_* env below is always the one in effect.
 *
 * globalSetup seeds the demo scenario synchronously (30-90s), builds
 * recovery opportunities, guarantees one PENDING_APPROVAL action and one
 * stored evaluation run, then writes e2e/.tmp/seed-state.json for the specs.
 *
 * Serial workers: every spec drives the same seeded scratch DB.
 */
export default defineConfig({
  testDir: "./e2e",
  outputDir: "./e2e/.tmp/artifacts",
  globalSetup: "./e2e/global-setup.ts",
  timeout: 120_000,
  expect: { timeout: 20_000 },
  workers: 1,
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: FRONTEND_BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `.venv\\Scripts\\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port ${BACKEND_PORT}`,
      cwd: "../backend",
      url: `${API_BASE_URL}/healthz`,
      env: {
        DATABASE_URL: SCRATCH_DATABASE_URL,
        APP_ENV: "dev",
        SIMULATION_MODE: "true",
        API_KEY,
        // pydantic-settings JSON-decodes list fields from env — a bare string
        // raises SettingsError at import time, so pass a JSON array.
        CORS_ORIGINS: JSON.stringify([FRONTEND_BASE_URL]),
        PYTHONUTF8: "1",
      },
      reuseExistingServer: true,
      timeout: 120_000,
    },
    {
      command: `npm run dev -- -p ${FRONTEND_PORT}`,
      url: FRONTEND_BASE_URL,
      env: {
        NEXT_PUBLIC_API_BASE_URL: API_BASE_URL,
        NEXT_PUBLIC_API_KEY: API_KEY,
      },
      reuseExistingServer: true,
      timeout: 240_000,
    },
  ],
});

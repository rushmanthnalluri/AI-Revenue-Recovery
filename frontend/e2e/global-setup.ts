/**
 * Global setup: seed the scratch API once so every spec sees real data.
 *
 * Steps (all idempotent — a re-run against an existing e2e_test.db is cheap):
 *   1. alembic upgrade head on the scratch DB (fresh files have no schema);
 *   2. wait for the scratch backend's /healthz;
 *   3. POST /api/v1/demo/scenario/upi_outage_demo — synchronous simulator
 *      seed + one anchored detection pass (30-90s on a fresh DB, seconds on
 *      a re-run thanks to run_idempotent);
 *   4. build recovery opportunities for the detected incident, then walk
 *      them by amount desc until the policy gate puts one into
 *      PENDING_APPROVAL (plan preview is a real gate evaluation, so this is
 *      deterministic — anything above the Rs 5,000 auto-execute ceiling or
 *      below the confidence floor qualifies);
 *   5. guarantee one stored evaluation run (small-scale two-arm harness);
 *   6. persist everything to e2e/.tmp/seed-state.json.
 */
import { execFile } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { promisify } from "node:util";

import { API_BASE_URL, API_KEY, SCRATCH_DATABASE_URL } from "./stack";
import type { SeedState } from "./seed-state";

const execFileAsync = promisify(execFile);

const SCENARIO = "upi_outage_demo";
const STATE_PATH = path.resolve(__dirname, ".tmp", "seed-state.json");

interface Paginated<T> {
  items: T[];
  total: number;
}

interface IncidentSummary {
  id: string;
}

interface OpportunitySummary {
  id: string;
  amount_paise: number;
}

interface OpportunityDetail {
  id: string;
  actions: { id: string; status: string }[];
}

interface RecoveryPlan {
  recommended_strategy_id: string | null;
  policy_preview: { outcome: string; reasons: string[] } | null;
}

interface ActionResponse {
  status: string;
  message: string;
}

interface EvaluationRunSummary {
  id: string;
  name: string;
  status: string;
}

async function api<T>(
  path: string,
  opts: { method?: "GET" | "POST"; body?: unknown; timeoutMs?: number } = {},
): Promise<T> {
  const { method = "GET", body, timeoutMs = 30_000 } = opts;
  const res = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-API-Key": API_KEY,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`${method} ${path} -> ${res.status}: ${text.slice(0, 300)}`);
  }
  return (text ? JSON.parse(text) : null) as T;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * The app never creates schema at startup — a fresh scratch DB needs the
 * alembic chain applied first (idempotent no-op when already at head).
 */
async function migrateScratchDb(): Promise<void> {
  const backendDir = path.resolve(__dirname, "..", "..", "backend");
  const python = path.join(backendDir, ".venv", "Scripts", "python.exe");
  const { stdout, stderr } = await execFileAsync(
    python,
    ["-m", "alembic", "upgrade", "head"],
    {
      cwd: backendDir,
      env: { ...process.env, DATABASE_URL: SCRATCH_DATABASE_URL, PYTHONUTF8: "1" },
      maxBuffer: 8 * 1024 * 1024,
    },
  );
  const tail = (stdout || stderr).trim().split("\n").slice(-2).join(" | ");
  console.log(`[e2e setup] alembic upgrade head: ${tail || "ok"}`);
}

async function waitForBackend(): Promise<void> {
  for (let attempt = 0; attempt < 60; attempt++) {
    try {
      await api("/healthz", { timeoutMs: 5_000 });
      return;
    } catch {
      await sleep(2_000);
    }
  }
  throw new Error(`scratch backend never became healthy at ${API_BASE_URL}`);
}

async function seedScenario(): Promise<string | null> {
  console.log(`[e2e setup] seeding demo scenario ${SCENARIO} (synchronous, can take 30-90s)…`);
  const started = Date.now();
  const res = await api<{ incident_id: string | null; skipped: boolean; detail: string | null }>(
    `/api/v1/demo/scenario/${SCENARIO}`,
    { method: "POST", body: {}, timeoutMs: 300_000 },
  );
  console.log(
    `[e2e setup] scenario seeded in ${Math.round((Date.now() - started) / 1000)}s ` +
      `(skipped=${res.skipped}) — ${res.detail ?? "no detail"}`,
  );
  if (res.incident_id) return res.incident_id;
  // List APIs default to the real merchant environment — the demo seed is
  // pinned to research, so the fallback lookup must scope explicitly.
  const list = await api<Paginated<IncidentSummary>>(
    "/api/v1/incidents?environment=research&page=1&page_size=1",
  );
  return list.items[0]?.id ?? null;
}

async function ensurePendingApproval(incidentId: string | null): Promise<string | null> {
  const existing = await api<Paginated<OpportunitySummary>>(
    "/api/v1/recovery/opportunities?environment=research&status=PENDING_APPROVAL&page=1&page_size=1",
  );
  if (existing.total > 0 && existing.items[0]) {
    console.log(`[e2e setup] reusing existing pending approval ${existing.items[0].id}`);
    return existing.items[0].id;
  }
  if (!incidentId) return null;

  await api("/api/v1/recovery/opportunities/build", {
    method: "POST",
    body: { incident_id: incidentId, actor: "system:e2e-setup" },
    timeoutMs: 180_000,
  });
  const all = await api<Paginated<OpportunitySummary>>(
    `/api/v1/recovery/opportunities?environment=research&incident_id=${encodeURIComponent(incidentId)}&page=1&page_size=100`,
  );
  const candidates = [...all.items].sort((a, b) => b.amount_paise - a.amount_paise).slice(0, 15);
  for (const opp of candidates) {
    const detail = await api<OpportunityDetail>(`/api/v1/recovery/${opp.id}`);
    // Skip opportunities that already have an action (e.g. approved by an
    // earlier suite run — duplicate protection would block a second one).
    if (detail.actions.length > 0) continue;
    const plan = await api<RecoveryPlan>(`/api/v1/recovery/${opp.id}/plan`);
    if (plan.policy_preview?.outcome !== "REQUIRES_APPROVAL") continue;
    const res = await api<ActionResponse>(`/api/v1/recovery/${opp.id}/execute`, {
      method: "POST",
      body: { actor: "system:e2e-setup" },
      timeoutMs: 60_000,
    });
    if (res.status === "PENDING_APPROVAL") {
      console.log(`[e2e setup] created pending approval on opportunity ${opp.id}`);
      return opp.id;
    }
  }
  console.warn("[e2e setup] WARNING: could not create a PENDING_APPROVAL action");
  return null;
}

async function ensureEvaluationRun(): Promise<string | null> {
  const existing = await api<Paginated<EvaluationRunSummary>>(
    "/api/v1/evaluation/runs?page=1&page_size=1",
  );
  if (existing.total > 0 && existing.items[0]) {
    console.log(`[e2e setup] reusing stored evaluation run ${existing.items[0].name}`);
    return existing.items[0].name;
  }

  const name = "e2e-seed";
  console.log("[e2e setup] triggering a small two-arm evaluation run (synchronous)…");
  const started = Date.now();
  try {
    await api("/api/v1/evaluation/run", {
      method: "POST",
      // Preset traffic density (2000 events/day) kept so detection scores;
      // 4 days at fraction 0.5 still contains the full UPI outage window.
      body: {
        name,
        evaluation_type: "end_to_end",
        scenario: SCENARIO,
        days: 4,
        events: 8_000,
        customers: 600,
      },
      timeoutMs: 300_000,
    });
    console.log(`[e2e setup] evaluation run finished in ${Math.round((Date.now() - started) / 1000)}s`);
    return name;
  } catch (err) {
    // The harness executes synchronously and can outlive any client timeout;
    // the stored row is the truth — poll for it below.
    console.warn(`[e2e setup] evaluation POST returned early (${String(err)}); polling the stored row…`);
  }
  for (let attempt = 0; attempt < 150; attempt++) {
    const runs = await api<Paginated<EvaluationRunSummary>>(
      "/api/v1/evaluation/runs?page=1&page_size=5",
    );
    const found = runs.items.find((r) => r.name === name);
    if (found && found.status !== "running") return found.name;
    await sleep(2_000);
  }
  console.warn("[e2e setup] WARNING: evaluation run never appeared");
  return null;
}

export default async function globalSetup(): Promise<void> {
  await migrateScratchDb();
  await waitForBackend();
  const incidentId = await seedScenario();
  console.log(`[e2e setup] incident: ${incidentId ?? "NONE"}`);
  const pendingOpportunityId = await ensurePendingApproval(incidentId);
  const evaluationRunName = await ensureEvaluationRun();

  const state: SeedState = {
    incidentId,
    pendingOpportunityId,
    evaluationRunName,
    seededAt: new Date().toISOString(),
  };
  mkdirSync(path.dirname(STATE_PATH), { recursive: true });
  writeFileSync(STATE_PATH, JSON.stringify(state, null, 2));
  console.log(`[e2e setup] state written to ${STATE_PATH}`);
}

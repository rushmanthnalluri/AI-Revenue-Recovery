# Test Quality Audit — PulseRecover (Phase 13)

Captured: 2026-09-02. Auditor: test-quality agent (Phases 13-14). COMPLETE.
Status vocabulary: WORKING / PARTIALLY_WORKING / BROKEN / MOCKED / SIMULATED / UNIMPLEMENTED / UNCERTAIN.

## 1. Backend suite run (this host, this audit)

- Command: `cd backend && .venv/Scripts/python -m pytest -q -p no:warnings` (log: `/tmp/audit_pytest_full.log`)
- Result: **971 passed, 0 failed** in **1343.10 s (22:23)** — exit 0. Run finished 2026-09-02 ~14:21 IST.
- Collected count is **971**, not the 968 recorded in baseline.md (suite grew by 3 since 2026-09-01) and not the **678** claimed by README.md:23,261,580,743 and docs/claim-matrix.md:12-13,46 — doc staleness, see §8.
- Runtime shape: ~7% of items (the `tests/demo/` module fixture, 10 seeded end-to-end scenario runs — `tests/demo/test_demo_scenarios.py:30-39`) accounts for roughly the first 15 minutes with no progress dots; the remaining ~93% finishes in ~7 minutes. Suite duration is dominated by simulator-seeded integration tests, not by unit-test count.
- Per-area collected counts: derived from grep of `def test_` (866 test functions in 111 files → 971 items after parametrize expansion), table in §4.

## 2. Frontend gates

Run in the working tree (`frontend/`), 2026-09-02 ~14:30 IST:

- `npx tsc --noEmit`: **WORKING** — exit 0, no output.
- `npm run lint`: **BROKEN in this working tree** — real exit 1 (log `/tmp/audit_lint.log`). Sole error: `scripts/rz-discover.mjs:2:22` `@typescript-eslint/no-require-imports`. That file is the **uncommitted scratch Playwright probe** flagged in baseline.md (`git status --porcelain` lists it; `git ls-files scripts/` is empty — nothing under `frontend/scripts/` is tracked). Proof the tracked tree is clean: `npx eslint . --ignore-pattern "scripts/rz-discover.mjs"` → exit 0. So the documented gate is red on the current checkout purely because of local scratch state; a clean clone lints green. Baseline's "frontend gates green as of 2026-09-01" is no longer true of this working tree.
- `npm run build`: **WORKING** — exit 0 (log `/tmp/audit_frontend_build.log`); Next.js 15 production build compiled, 11 app routes generated (8 static, 2 dynamic), shared First Load JS 102 kB. No warnings of substance.

## 3. Frontend e2e inventory (documented, not run)

Source: `frontend/playwright.config.ts`, `frontend/e2e/*.spec.ts`, `frontend/e2e/stack.ts`, `frontend/e2e/global-setup.ts`. Per assignment: inventoried, NOT executed.

- 7 spec files, **9 Playwright tests total**, chromium only, `workers: 1`, serial (`playwright.config.ts:23-31`).
- Coverage per spec (test titles from grep of `*.spec.ts`):
  - `command-center.spec.ts` (3): real-merchant not-connected empty state + truthful badge; research mode renders seeded dataset under synthetic banner; payments/settings reflect connection state.
  - `backend-down.spec.ts` (1): outage surfaces unreachable-error panel.
  - `incidents.spec.ts` (1): env-scoped incidents list opens seeded research incident.
  - `investigation.spec.ts` (1): AI investigation renders facts/inference/recommended-action zones.
  - `recovery.spec.ts` (1): recovery pipeline + human approval flow.
  - `evaluation.spec.ts` (1): evaluation lab renders run metrics.
  - `audit.spec.ts` (1): audit trail env-scoped with filtering.
- Stack assumptions (`e2e/stack.ts:10-16`): scratch FastAPI on **:8001**, Next dev server on **:3100**, `API_KEY="dev-key"`, scratch DB `backend/e2e_test.db` (gitignored). The port rationale comment states 8000 is taken by an unrelated Docker container on the developer machine — i.e. the e2e harness is tuned to one specific workstation's port map.
- Playwright spawns the backend with a **Windows-only path**: `` `.venv\\Scripts\\python.exe -m uvicorn` `` (`playwright.config.ts:41`, backslash + `Scripts` layout). On macOS/Linux clones this webServer command fails outright.
- `global-setup.ts` seeds via the **simulator** demo scenario `upi_outage_demo` (research environment), guarantees one PENDING_APPROVAL action and one stored evaluation run. So all 9 tests exercise research/simulated data; the "real merchant mode" test only asserts the **not-connected empty state** — no e2e test drives real_test data or the live deployment.
- No deployed-browser e2e exists (nothing points at `*.onrender.com`; `stack.ts` hardcodes localhost).

## 4. Test suite structure / inventory

Backend: **111 test files, ~866 test functions** (grep `def test_` per dir, 2026-09-02), collected as **971 items** after parametrize expansion (live run, §1). Config: `backend/pytest.ini` = `pythonpath = .`, `testpaths = tests` — no markers, no skip rules, no coverage config.

| Area | test files | test funcs | Subject |
|------|-----------:|-----------:|---------|
| recovery/ | 12 | 109 | executor, failure modes, retries, subscriptions, principal binding |
| security/ | 10 | 93 | auth boundaries, secret leakage, input abuse, gateway inconsistency |
| policy/ | 4 | 89 | policy engine allow/deny, validation |
| detection/ | 7 | 74 | detectors, floors/dedup, night regime, comparisons |
| diagnosis/ | 7 | 64 | training, features, calibration, split, service, prodframe, rescope |
| revenue/ | 8 | 55 | statistics, classification |
| agent/ | 7 | 54 | LLM seam, heuristic, injection corpus, tools, ranked candidates |
| razorpay/ | 3 | 48 | gateway client, webhooks, simulated gateway |
| merchant/ | 3 | 32 | sync service, probe (all via `httpx.MockTransport`, see §6) |
| worker/ | 4 | 28 | worker ticks, delayed retries, reconcile |
| invariants/ | 7 | 27 | concurrency, fail-closed confidence, refund-no-transport |
| integration/ | 7 | 25 | API-level flows: full loop, demo, dashboard, safe-stop, run auditing |
| environment/ | 7 | 25 | real_test vs research isolation, executor environments |
| evaluation/ | 6 | 25 | harness, reproducibility (reads committed artifact — §6) |
| insights/ | 3 | 20 | insights API |
| simulator/ | 6 | 16 | determinism, ground truth, taxonomy, volume, seed CLI |
| agenteval/ | 1 | 9 | agent metric evals |
| provenance/ | 1 | 7 | source-type provenance |
| demo/ | 1 | 6 | demo scenario scripts |
| architecture/ | 1 | 3 | import-boundary rules |
| root tests/ | 6 | ~57 | smoke, db_url, export, health aggregation, model indexes, real_data_workflow |

Naming caveat: `tests/integration/` = API-integration via `TestClient` against in-memory SQLite — **not** third-party integration tests. `test_real_data_workflow.py` is named "real" but is fully mocked (see §6).

## 5. Per-area analysis: what each area PROVES vs NOT-proves

Method: file/dir inventory + conftest wiring + targeted reads (conftest.py, merchant/conftest.py, razorpay/conftest.py, integration/conftest.py, test_real_data_workflow.py, test_smoke.py, test_model_indexes.py). "PROVES" = a green suite gives real confidence; "NOT-proves" = the suite passing says nothing about it.

- **All areas, shared fixture stack** (`tests/conftest.py:36-65`): every API/ORM test runs against in-memory SQLite built by `Base.metadata.create_all` — **not** the alembic chain. PROVES: ORM/API/service logic on a metadata-accurate schema. NOT-proves: that the deployed Postgres schema (head `a83af82e8438`) matches ORM metadata; SQLite-vs-Postgres behavior gaps (JSON types, FK enforcement dialect, concurrency). Partial counter-evidence: `tests/provenance/test_provenance.py:265-322` and `tests/environment/test_migration.py:36-107` do run real `alembic upgrade` against scratch SQLite — so the migration chain itself is tested, but only on SQLite and only for the revisions those files target.
- **smoke (root)**: PROVES health endpoints, error envelope, API-key guard (401 without/with wrong key), OpenAPI serves key routes, ORM round-trip (`test_smoke.py`). NOT-proves: anything about real gateway connectivity — `test_system_health` asserts gateway detail == `"simulator"` under pinned-empty keys.
- **merchant/ (sync)**: PROVES pagination slicing, per-entity failure degradation, quarantine of invalid entities, idempotent re-sync, probe auth-failure mapping — against a fixture server (`tests/merchant/conftest.py:128-189`, `FakeRazorpayAPI` on `httpx.MockTransport`, `sleep=lambda _s: None`). NOT-proves: real Razorpay payload drift (fixture shapes are hand-maintained from docs §C), real rate limiting/backoff (sleep is stubbed), real auth header behavior, network failure modes beyond those programmed.
- **razorpay/**: webhook tests use genuinely valid HMAC signatures against a `SimulatedPaymentGateway` (`tests/razorpay/conftest.py:21-47`) — PROVES signature verification + dispatch logic. `test_client.py` covers the read client via MockTransport. NOT-proves: live Razorpay API contract, real webhook delivery/retry semantics.
- **recovery/** (largest area, 109 funcs): PROVES executor state machine, approval gates, policy interaction, duplicate protection, subscription recovery status mapping. Gateway effects run through the simulated gateway or patched `_request` (e.g. `test_real_data_workflow.py:447` patches `RazorpayGateway._request` directly — the "real gateway" test never leaves the process). NOT-proves: that a payment link can actually be created on Razorpay, webhook-verified recovery completion against live events.
- **policy/ security/ invariants/**: PROVES fail-closed validation, auth boundaries, secret-non-leakage in responses (against MockTransport fixtures), concurrency invariants on SQLite. NOT-proves: Postgres-level race behavior (SQLite StaticPool serializes), production API-key strength (tests use `dev-key`).
- **detection/ diagnosis/ revenue/ evaluation/ agent*/**: PROVES detector math on synthetic/simulator-generated frames, training pipeline determinism, calibration, harness reproducibility. Diagnosis tests train a tiny synthetic model per session (`tests/diagnosis/conftest.py:21-29`) — they do NOT validate the shipped model artifact's quality. `tests/evaluation/test_reproducibility.py:102` does read the committed `artifacts/diagnosis_active.json` — the one test-file dependency on a committed binary artifact (present in a fresh clone per `.gitignore:34-36` exceptions; verified in reproduction.md).
- **simulator/ demo/ environment/**: PROVES simulator determinism/ground-truth, demo scenario scripts, real_test-vs-research row isolation. These underpin the demo path, not production data quality.
- **worker/**: PROVES tick scheduling, delayed-retry due-time logic, reconcile behavior with stubbed clocks. NOT-proves: long-running worker behavior, restart recovery, Postgres job contention.
- **architecture/** (3 funcs): PROVES import-boundary rules via static checks. Useful guardrail; no runtime meaning.

## 6. Mock-induced false confidence

Evidence chain (all path:line verified above):

1. **Env pinning makes the suite hermetic by construction** — `tests/conftest.py:17-23` sets `RAZORPAY_KEY_ID/SECRET/WEBHOOK_SECRET=""`, `SIMULATION_MODE=false`, `LLM_PROVIDER=none`, `OPENAI_API_KEY=""` before any app import. Docstring (lines 10-16) states the intent: the repo-root `.env` may carry live test-mode keys and the pins "keep tests off the real Razorpay API". Consequence: the suite can *never* fail due to a broken real integration — a green run is fully consistent with the live Razorpay path being broken.
2. **All gateway HTTP is MockTransport or simulator**: `tests/merchant/conftest.py:186` (`httpx.MockTransport(fake_api.handler)`), `tests/razorpay/conftest.py:26` (`SimulatedPaymentGateway`), `tests/integration/conftest.py:43`, `tests/test_real_data_workflow.py:45-57` (own `MockTransport` class).
3. **Misleading name**: `tests/test_real_data_workflow.py` — module docstring claims "verify … real Razorpay sync, webhook ingestion, and recovery execution end-to-end" (lines 1-6) but every network call is mocked; `test_recovery_creates_payment_link_via_real_gateway` patches `gateway._request` (line 447) and asserts against the patched return value. It proves call-shape and provenance bookkeeping only.
4. **No opt-in real-integration marker exists**: grep for `skipif|REAL_RAZORPAY|LIVE_` across `tests/` finds zero hits — there is no automated or semi-automated real-API lane at all. Real Razorpay verification (6 payments, webhook intake, 401s) was done manually per baseline.md.
5. **Schema via `create_all`, not migrations** (see §5) — migration drift vs ORM metadata would not fail the suite except where the two alembic-scratch test files happen to look.
6. **Fixture payload shapes are hand-maintained** from docs (`merchant/conftest.py:1-8` cites docs/razorpay-integration.md §C/§B). If Razorpay changes a payload or the docs are wrong, fixtures and code can be consistently wrong together.

## 7. Missing areas

- **Real-integration lane**: UNIMPLEMENTED as automation — manual-only (baseline.md §Integrations). No marker, no CI hook, no runbook reference in pytest config.
- **Deployed-browser e2e**: UNIMPLEMENTED — Playwright stack is localhost-only, research/simulator data only (§3).
- **Clean-env/CI reproduction**: no CI config exists — Glob for `.github/**` returns zero matches (2026-09-02); the suite is only known to run on the developer workstation. See reproduction.md for what a clean machine actually hits (answer: backend path works; edges break).
- **Load/performance**: UNIMPLEMENTED — no load tests, no concurrency tests against Postgres (only SQLite StaticPool), no worker-throughput tests.
- **Postgres-specific coverage**: UNIMPLEMENTED — everything runs on SQLite; Neon-specific behavior (serverless suspend/resume, pooled connections) untested.
- **Contract test vs `contracts/openapi.json`**: UNIMPLEMENTED — grep finds no test reading the committed contract; `tests/test_export.py` is only about export-endpoint timestamps, and `test_smoke.py:40` checks routes exist in the served schema without diffing against `contracts/openapi.json`. Contract drift would not fail the suite.
- **Frontend unit tests**: UNIMPLEMENTED — frontend gates are tsc + eslint + build only (`package.json` scripts: dev/build/start/lint/typecheck/test:e2e; no unit-test runner, no `*.test.*` under `frontend/src/` per Glob 2026-09-02).

## 8. Doc-vs-reality mismatches (test claims)

Every entry verified against command output on 2026-09-02:

| Claim | Where | Reality | Status |
|---|---|---|---|
| "678 backend tests" | README.md:23, README.md:261, README.md:580, README.md:743 | **971 passed** (this run, §1) | STALE (drift +293) |
| "678 tests collected … 678 passed 2026-08-28" | docs/claim-matrix.md:12-13, row 1.1 (:46) | 971 collected/passed now | STALE (was true 2026-08-28) |
| "81 policy tests" | docs/claim-matrix.md:47 | 89 test functions in `tests/policy/` (grep 2026-09-02); collected items higher with parametrize | STALE |
| "88 security tests" | docs/claim-matrix.md:48, :242 | 93 test functions in `tests/security/` | STALE |
| "47 razorpay tests" | docs/claim-matrix.md:49 | 48 test functions in `tests/razorpay/` | STALE |
| "7 Playwright e2e tests" | docs/claim-matrix.md:52 (row 1.7) | **9** `test(` across 7 spec files (§3) | STALE |
| "10 demo proof tests (5 scenarios × 2 runs)" | docs/claim-matrix.md:50 | 6 test functions in `tests/demo/` (5 parametrized scenarios + determinism = 6+ items; README:446 says 10 tests) — count shape differs from claim, low materiality | UNCERTAIN |
| "968 backend tests … last full run: 968 passed" | docs/audit/baseline.md:47 | 971 passed 2026-09-02 | STALE by 3 (grew since 2026-09-01) |

None of these break the suite; all are freshness drift in hand-maintained count claims. The claim-matrix's mechanism (collect-only recount) exists but has not been re-run as counts grew.

## 9. Overall assessment

- Backend suite: **WORKING** and meaningful for application logic — 971 green, broad area coverage, strong invariant/security/policy lanes.
- Integration confidence: **SIMULATED** — every byte of "Razorpay" traffic in the suite is MockTransport or the in-memory twin; the suite is hermetic by design (env pins) and cannot detect a real-integration regression. Real-Razorpay verification is manual-only (no marker, no CI).
- Frontend gates: tsc WORKING; lint BROKEN-by-scratch-file in this tree (clean clone green); build WORKING. Frontend has no unit tests and 9 localhost-only e2e tests on simulated data.
- Schema fidelity: PARTIALLY_WORKING — two files exercise real alembic chains on SQLite; the bulk of the suite builds schema from ORM metadata on SQLite, so Postgres/Neon-specific drift is invisible.
- Load/performance, deployed-browser e2e, CI: UNIMPLEMENTED.

## Appendix: raw command outputs

- `pytest -q -p no:warnings` (backend, working repo): final lines `................................... [100%]` / `971 passed in 1343.10s (0:22:23)`, pipeline exit 0. Full log: `/tmp/audit_pytest_full.log`.
- `npx tsc --noEmit` (frontend): no output, exit 0.
- `npm run lint` (frontend): real exit 1; output tail = `D:\Razorpay\frontend\scripts\rz-discover.mjs / 2:22 error A `require()` style import is forbidden @typescript-eslint/no-require-imports / ✖ 1 problem (1 error, 0 warnings)` (log `/tmp/audit_lint.log`). `npx eslint . --ignore-pattern "scripts/rz-discover.mjs"` → exit 0, no output.
- `npm run build` (frontend): exit 0; route table lists 11 app routes, First Load JS shared 102 kB (log `/tmp/audit_frontend_build.log`).
- Inventory greps: `def test_` counts per directory (866 total; per-area numbers in §4); `git ls-files backend/artifacts/` → 3 committed artifact files; `git ls-files frontend/scripts/` → empty.
- Clone-side outputs (test slice, seed, detection, demo): see reproduction.md appendix.

# Clean-Room Reproduction Audit — PulseRecover (Phase 14)

Captured: 2026-09-02. Auditor: test-quality agent (Phases 13-14). COMPLETE.
Verdict vocabulary per step: WORKS / FAILS / NEEDS-UNDOCUMENTED-KNOWLEDGE / UNCERTAIN.

## Method

- Fresh clone: `git clone --depth 1 https://github.com/rushmanthnalluri/AI-Revenue-Recovery` into `D:/cleanroom-repro` (outside the working repo).
- Fresh venv from clean Python (`/c/Users/rushm/AppData/Local/Programs/Python/Python314/python.exe`, 3.14.5).
- Install `backend/requirements.txt`, fresh sqlite, `alembic upgrade head`, boot uvicorn, probe `/healthz`, run a test slice.
- Then walk the README research/simulator path (seed.py → detection/dashboard → demo_run.py scenario B) as a new user would.
- Heavy steps run strictly sequentially, never overlapping the working-repo suite.

## Step log

| # | Step | Verdict | Evidence |
|---|------|---------|----------|
| 1 | git clone --depth 1 | WORKS | clone of public repo into `D:/cleanroom-repro/AI-Revenue-Recovery`; `git rev-parse HEAD` = `dcef95a57ca81db25700902fb9a4a4358439628a` (== working repo HEAD). 584 files. |
| 2 | fresh venv (Python 3.14) | WORKS | `python -m venv backend/.venv` → Python 3.14.5, pip 26.1.1. Caveat: the *documented* shortcut to this interpreter is a hardcoded dev-machine path (Makefile:10) — see findings. |
| 3 | pip install backend/requirements.txt | WORKS | exit 0; all 46 pinned packages install with cp314 wheels on Python 3.14.5 (no compilation); installed set matches baseline pins (fastapi 0.141.1, SQLAlchemy 2.0.52, pydantic 2.13.4, alembic 1.19.1, httpx 0.28.1, scikit-learn 1.9.0, pandas 3.0.5, psycopg[binary] 3.3.4, pytest 9.1.1). |
| 4 | fresh sqlite + alembic upgrade head | WORKS | `cp .env.example .env` at clone root, then `cd backend && alembic upgrade head` → 4 revisions applied (`77c0efef3d84` → `f3a9c1e7b204` → `b4e7a1c2d305` → `a83af82e8438`), fresh `backend/pulserecover.db` (585,728 bytes), head == production head per baseline. |
| 5 | boot uvicorn + GET /healthz | WORKS | `uvicorn app.main:app` on :8123 healthy after 10 s. `/healthz` → `{"status":"ok"}`; `/api/v1/system/health` → database ok, policy_engine ok (`1.0+sha256.84dd80a17d02`), llm_provider disabled/none, gateway `simulator`, worker disabled; `/readyz` ok. Live-response capture 2026-09-02T08:43:44Z. |
| 6 | test slice in clone | WORKS | `pytest tests/test_smoke.py tests/diagnosis tests/evaluation/test_reproducibility.py tests/merchant tests/razorpay -q` in the clone venv → **159 passed in 76.45 s**, exit 0. Deliberately includes the artifact-dependent lane: `tests/evaluation/test_reproducibility.py:102` reads the committed `artifacts/diagnosis_active.json`, and the diagnosis suite trains tiny models in-session — so the committed model artifact + ML stack work on a clean machine. (Full 971-test suite not re-run in the clone; the working-repo run is in test-coverage.md §1.) |
| 7 | README research/simulator path | WORKS | seed.py + detection/dashboard + demo_run.py scenario B — details below. |

## Step 7 detail — README research/simulator path

- `scripts/seed.py --help` OK; seeded `--events 8000 --days 5 --customers 600 --scenario upi_outage_demo --force` in 2.5 s (runtime_ms 975, 17,209 rows/s, incidents injected — JSON output captured).
- Against the booted app: `POST /api/v1/detection/run {}` → completed, `environment:"real_test"`, `anomalies_detected:0`, detail "no terminal payment events in scope" (correct: seed lands in research); `POST … {"environment":"research"}` → completed, 19 outcomes scored, 0 anomalies (legitimate for the tiny window); `GET /api/v1/dashboard/summary?environment=research` → payments_observed 19, SR 0.9474. Mechanical loop (seed → detect → dashboard) works end-to-end on a clean clone.
- NOTE: detection defaults to `real_test`, and the README quickstart never tells a new user the seed lands in `research` — a fresh user sees an empty detection result until they discover the environment knob. Minor docs gap, not a break.
- **Demo CLI:** `scripts/demo_run.py --scenario B --db <scratch>` → full closed loop in **46.3 s**: detection → investigation (heuristic reasoner, `LLM_PROVIDER=none`) → 893 opportunities → plan preview ALLOWED → execute ₹501 → gate `auto_execute.ok` → webhook `payment.captured` (HMAC valid, deduped) → action RECOVERED → 8-row audit chain printed. Matches README:462's scenario-B claim (₹501, confidence 0.98 ≥ 0.85, no human). Uses the committed diagnosis artifact (README:622-630 claim re-verified in practice). Verdict: WORKS.
- Minor cosmetic: the fresh venv prints a `StarletteDeprecationWarning` (httpx TestClient) on CLI startup — harmless, pin-identical to the working repo.

## Committed model artifacts in clone?

WORKS — `git ls-files backend/artifacts/` in the clone lists exactly 3 files: `diagnosis_active.json`, `diagnosis_logistic_regression_v20260826T234303Z-c5434878.joblib`, `diagnosis_random_forest_v20260828T013109Z-77a4ef3b.joblib`. The pointer (`backend/artifacts/diagnosis_active.json`) references the committed random_forest joblib, and step 6 proves the artifact-dependent tests pass in the clone. The working repo's `backend/artifacts/` additionally carries ~45 untracked local files (calib.db, scratch/verify DBs, CSVs, logs, `repro/`, `multi_anchor/`) — local-only experiment state, correctly gitignored (`.gitignore:29-36`). Clone also has `policies/default.yaml`, `ml/experiments/` records, and only `.env.example` (no `.env`).

## Dependency findings (hidden files / env / hardcoded paths / artifacts / DB state)

What the working repo (`D:/Razorpay`) has that the clean clone does NOT, and whether anything depends on it:

1. **Hardcoded developer path — Makefile:10** (`make setup`): invokes `"/c/Users/rushm/AppData/Local/Programs/Python/Python314/python.exe" -m venv backend/.venv`. Verdict: **FAILS** on any machine except this one (portability break in a documented entry point — README:593 advertises `make setup`). Not hit when following the README's plain `python -m venv .venv` path (what I used, via the same interpreter binary by absolute path).
2. **Windows-only Playwright webServer command** (`frontend/playwright.config.ts:41`): `` `.venv\\Scripts\\python.exe -m uvicorn` `` — backslash + `Scripts` layout fails on macOS/Linux. Also `e2e/stack.ts:1-16` encodes this workstation's port occupancy (:8001/:3100 because :8000 is an unrelated Docker container here). Verdict: NEEDS-UNDOCUMENTED-KNOWLEDGE off-Windows; WORKS on this machine.
3. **Repo-root `.env` (untracked, real test-mode keys)**: absent in clone; NOT needed for boot/tests/simulator (`.env.example` defaults suffice — proven by steps 4-7, and tests pin env in `tests/conftest.py:17-23`). Needed only for the real-Razorpay lane, which is documented (README §What-is-real, docs/razorpay-integration.md). Verdict: documented dependency, WORKS as designed.
4. **`backend/artifacts/` untracked local state**: see "Committed model artifacts" above. The untracked extras are only needed to *regenerate* ML experiments, not to run anything. `.gitignore:29-36` and `.dockerignore:17-20` allowlists match README:622-630's claim — VERIFIED accurate. Verdict: WORKS.
5. **Existing DB state (`backend/pulserecover.db`, `e2e_test.db`)**: not needed — `alembic upgrade head` rebuilds schema from scratch (step 4); the app never creates schema at boot (by design, per `frontend/e2e/global-setup.ts:91-93` comment). E2E scratch DB is recreated by Playwright. Verdict: WORKS.
6. **`ml/experiments/` records**: committed, present in clone (agent/, detection/, diagnosis/, canonical_spec.json, multi_anchor/). Reproducibility claims referencing them are checkable in a clean clone. Verdict: WORKS.
7. **Frontend repro**: NOT WALKED in the clone (assignment scoped repro to backend; frontend gates were run in the working tree — see test-coverage.md §2). `npm install` + build in a clean clone: UNCERTAIN.
8. **Stale doc counts**: README/claim-matrix claim 678 backend + 7 e2e tests; reality 971 + 9 (test-coverage.md §8). Does not block reproduction but misleads a reproducer about expected suite size/duration.
9. **Leftover from previous audit swarm**: a stray process from an earlier clean-room attempt (`/tmp/pulserecover-cleanroom/backend/.venv/Scripts/python`, started 10:35, still alive at 14:08 per `ps`) was found on this host. Not a repo property; noted for host hygiene only. (I did not kill it — outside my read-only mandate; my own clean-room lives at `D:/cleanroom-repro` and its uvicorn was stopped after probing.)

## Overall verdict

The **backend README reproduction path works on a clean machine** with one documented-env copy and no hidden knowledge: clone → venv → install → `cp .env.example .env` → alembic → uvicorn → healthz → seed → detection/dashboard → demo scenario → test slice — every step WORKS (evidence above). The breaks are at the edges: `make setup` (hardcoded dev-machine Python path) FAILS elsewhere; Playwright e2e is Windows-this-machine-shaped (NEEDS-UNDOCUMENTED-KNOWLEDGE elsewhere); frontend clean-clone install UNCERTAIN (not walked); hand-maintained test-count claims in README/claim-matrix are STALE (678 vs actual 971; 7 vs actual 9 e2e).

## Appendix: raw command outputs

- Backend suite (working repo): `971 passed in 1343.10s (0:22:23)` — full log `/tmp/audit_pytest_full.log` (2-line progress format, `-q`).
- pip install (clone): `Successfully installed Mako-1.4.1 … fastapi-0.141.1` (46 packages), `PIP_EXIT=0`.
- alembic (clone): `Running upgrade  -> 77c0efef3d84, initial schema` … `Running upgrade b4e7a1c2d305 -> a83af82e8438, worker outbox, audit hash chain, commerce hot-path indexes`, `ALEMBIC_EXIT=0`.
- healthz (clone): `{"status":"ok"}`; system/health: `{"status":"ok","version":"0.1.0","app_env":"dev","simulation_mode":true, … "gateway":{"status":"ok","detail":"simulator"}, "worker":{"status":"disabled","detail":"WORKER_ENABLED=false"}}` at 2026-09-02T08:43:44Z.
- Test slice (clone): `159 passed in 76.45s (0:01:16)`.
- Frontend gates (working tree): tsc exit 0; `npm run lint` real exit 1 (sole error `scripts/rz-discover.mjs:2:22 no-require-imports`, untracked scratch file; `eslint . --ignore-pattern scripts/rz-discover.mjs` exit 0); `npm run build` exit 0, 11 routes (full log `/tmp/audit_frontend_build.log`).
- Demo scenario B (clone): `[POLICY] gate: ALLOWED (rules: auto_execute.ok)` … `[VERIFY] webhook payment.captured (HMAC signature valid, event id deduped) -> action RECOVERED - Rs 501 recovered` … `real 0m46.322s`.

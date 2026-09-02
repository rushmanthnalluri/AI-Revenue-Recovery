# Repository Map — PulseRecover (Audit Phase 1)

Captured: 2026-09-02 by audit agent (repository forensics). Read `docs/audit/baseline.md` first.
Classification vocabulary: **PRODUCTION-CRITICAL** (live request/worker path), **SUPPORT** (needed to run/deploy), **RESEARCH-ONLY**, **DEMO-ONLY**, **DEAD/UNUSED**.
Every claim carries path:line evidence. UNCERTAIN where unverifiable.

## 0. Executive summary of execution topology

One FastAPI process (modular monolith, ADR 0001) serves a Next.js browser UI and a Razorpay webhook ingress. A single optional in-process worker shares the process. The simulator and evaluation harness run in the same codebase but are reachable only via explicit triggers (demo router, scripts, evaluation endpoint) — nothing runs them on a schedule.

```
Browser (Next.js :3000, client-side react-query, X-API-Key on every request)
   |  REST /api/v1/*  +  /webhooks/razorpay (from Razorpay, HMAC-signed)
   v
FastAPI :8000  (backend/app/main.py — factory + 5 middlewares + router auto-discovery)
   |- 15 routers (api/v1/*) — the composition root, owns ALL commits
   |- services/* (13 packages) — business core, flush-only writers
   |- models/ — 24 tables, SQLAlchemy 2, portable types (ADR 0002)
   |- worker (opt-in, WORKER_ENABLED) — 3 due-driven units per 30s tick
   v
SQLite file (dev default) | Neon Postgres (Render prod) | postgres:16 (compose)
   ^
Razorpay Test Mode REST (outbound: gateway writes + sync reads) & webhooks (inbound)
```

Scale facts: backend app ~24.2k LOC Python (`wc -l app/**/*.py`), 131 test files (968 tests green per baseline §Commands), frontend src ~100 TSX/TS files, 24 tables, 4 alembic revisions (head `a83af82e8438`, baseline:24), 42 OpenAPI paths (baseline:50).

## 1. `backend/app` — application core

### 1.1 Entry & wiring — PRODUCTION-CRITICAL

- `main.py` (233 lines): app factory `create_app()` (:176). Router auto-discovery: pkgutil-scans `app.api.v1`, includes any module-level `router` (:147-155). Middleware outermost→innermost: CORS → RequestId → AccessLog → RateLimit → ApiKey (:190-202). Rate limits: in-memory sliding window, webhooks 120/60s, mutating /api/v1 60/60s, per-process (:45,:85-116). ApiKeyMiddleware guards mutating /api/v1, fails CLOSED (503) when API_KEY unset, exempts `/api/v1/demo` + `/api/v1/detection` when `APP_ENV != "prod"` (:119-144). Lifespan starts worker only when `WORKER_ENABLED=true` (:158-173). Global error envelope `{error:{code,message,request_id}}` (:48-52, :206-228).
- `config.py`: pydantic-settings; `.env` resolved at repo root regardless of CWD (:16,21). Defaults: SQLite URL (:27), `APP_ENV="dev"` with `prod` reachable (:28-30), `SIMULATION_MODE=false`, `LLM_PROVIDER="none"` (:39), `API_KEY="dev-key"` (:45), `WORKER_ENABLED=false` (:56).
- `db.py`: engine + `SessionLocal` (`autoflush=False, expire_on_commit=False`, :95). `_normalize_url` maps `postgres(ql)://` → `postgresql+psycopg://` (:46-56). PG connect timeout 3s (:59-67). SQLite FK pragma per connection (:70-85). `TZDateTime` forces tz-aware UTC both ways (:19-35).
- `api/deps.py`: THE single gateway DI seam `get_gateway_dependency` (:27-29) + KYA-lite `Principal` derived from the X-API-Key (demo-grade cohort identity, not per-user authN; :47-117).
- `ports.py`: the integration contract — `PaymentGateway`, `PolicyEngineProto`, `ReasonerProto`, `NotificationSender` protocols + enums (`ActionType` 9 values, `RecoveryStatus` 13 states, :26-87) + dataclasses (`ActionContext`, `PolicyDecision`, `StrategyCandidate`, `EvidenceBundle`). Money is integer paise (:13).
- `ids.py`: prefixed uuid4 id helpers (`inc_`, `pay_`, `act_`, `gwr_`, `aud_`, ...).
- `logging.py`: stdlib JSON logs, one object/line, `request_id_ctx` contextvar (:14), secret-ish key redaction (`*secret*/*key*/*token*/...`, :16-24).

Dependency direction: every module above is a shared contract — importable by all (tests/architecture/test_boundaries.py:11).

### 1.2 API layer `api/v1/` — 15 routers, 42 endpoints — PRODUCTION-CRITICAL

The composition root: routers construct services, own the transaction commit, and map domain errors to HTTP. Services flush; routers commit (verified: merchant.py:81,108; detection.py:66; demo.py:272; webhooks.py:102,119).

| Router (file) | Prefix | Endpoints | Calls | Class |
|---|---|---|---|---|
| health.py | — | `GET /healthz`, `/readyz`, `/api/v1/system/health` (:34,:39,:51) | db check, policy load, gateway_mode, worker supervisor | PRODUCTION-CRITICAL |
| webhooks.py | — | `POST /webhooks/razorpay` (:60) | gateway.verify_webhook_signature → persist WebhookEvent → recovery.webhook_handlers.dispatch_event | PRODUCTION-CRITICAL |
| merchant.py | /api/v1/merchant | connection, sync, sync/enable, sync/disable (:56,:64,:112,:121) | SyncService (probe + full sync) | PRODUCTION-CRITICAL |
| dashboard.py | /api/v1/dashboard | summary, timeseries (:147,:257) | read-only aggregates over models | PRODUCTION-CRITICAL |
| incidents.py | /api/v1/incidents | list, detail (:74,:263) | models + insights (read-time) | PRODUCTION-CRITICAL |
| agent.py | /api/v1/incidents | investigate, investigation (:93,:123) | AgentService | PRODUCTION-CRITICAL |
| recovery.py | /api/v1/recovery | opportunities, approvals-summary, build, reconcile, {id}, plan, execute, approve, reject, escalate, cancel (:331-:745) | OpportunityBuilder, StrategyGenerator, RecoveryExecutor, run_reconciliation | PRODUCTION-CRITICAL |
| payments.py | /api/v1/payments | list (:26) | models | PRODUCTION-CRITICAL |
| detection.py | /api/v1/detection | run (:23) | detection.engine.run_detection | PRODUCTION-CRITICAL (only detection trigger besides demo) |
| policy.py | /api/v1/policy | backtest (:28) | policy.backtest | SUPPORT |
| audit.py | /api/v1/audit | list, verify (:26,:80) | models + audit.verify.verify_chain | PRODUCTION-CRITICAL |
| evaluation.py | /api/v1/evaluation | runs, run detail, metrics, run (:46,:66,:78,:118) | evaluation.runner | RESEARCH-ONLY |
| demo.py | /api/v1/demo | scenarios, scenario/{name}, reset (:152,:159,:254) | simulator.cli.run_idempotent + anchored run_detection; env-scoped bulk delete | DEMO-ONLY |
| export.py | /api/v1/export | audit, incidents, recovery, payments, summary (:66-:547) | models (CSV/JSON) | PRODUCTION-CRITICAL |

### 1.3 Models `models/` — 24 tables — PRODUCTION-CRITICAL (shared data contract)

`grep __tablename__ app/models/*.py` = 24 tables (architecture.md §1 claims 21 — stale, see architecture-actual.md §6).

- `base.py`: `enum_col`, `TimestampMixin`, **provenance**: `SOURCE_TYPE_SIMULATOR|RAZORPAY_TEST|RAZORPAY_LIVE` (:41-45), `ENVIRONMENT_REAL_TEST="real_test"` / `ENVIRONMENT_RESEARCH="research"` (:52-54), `source_types_for_environment()` — the only sanctioned mapping (:62-70), `EnvironmentMixin` (default `research` = fail-safe direction, :73-88), `ProvenanceMixin` (:91-112).
- `commerce.py`: merchants, customers, orders, payments, payment_events, subscriptions. Uniqueness: `payments.gateway_payment_id` UNIQUE (:99), `(source_type, external_id)` UNIQUE (:77) — the sync idempotency contract; orders/subscriptions gateway ids UNIQUE (:63,:149). Commerce rows have NO environment column — environment is derived from source_type (base.py:47-51).
- `incidents.py`: incidents, incident_evidence, diagnoses.
- `recovery.py`: recovery_opportunities, recovery_strategies, recovery_actions (`gateway_request_id` UNIQUE :118 — one gateway mutation per action), policy_decisions.
- `notification.py`: notification_outbox (worker outbox).
- `system.py`: audit_logs (hash-chained via session `before_flush` hook :79-117), webhook_events (`gateway_event_id` UNIQUE :127 — delivery dedup).
- `evaluation.py`: experiments, model_predictions, evaluation_runs, simulator_runs, simulator_ground_truth, agent_reports.
- `sync.py`: sync_runs, connection_state (singleton row).

### 1.4 Schemas `schemas/` — Pydantic v2 request/response — PRODUCTION-CRITICAL

One module per router domain (14 files). The frontend contract; `DetectionRunRequest` is the richest (17 controls incl. `environment: Literal["real_test","research"] = "real_test"`, schemas/detection.py:45).

### 1.5 Services `services/` — 13 packages — the business core

Convention: **services flush, never commit; the API layer commits** (stated in executor.py:33-34, webhook_handlers.py:47-48, audit.py:18-20, merchant/service.py:19). Documented exceptions: `recovery.reconcile` commits per repaired unit (reconcile.py:16-22), `worker` commits per row (worker.py:161,195), `AgentService.investigate` self-commits (agent/service.py:11,120,156).

| Package | Responsibility | Key files & evidence | Imports (bounded by test_boundaries.py) | Class |
|---|---|---|---|---|
| policy | Deterministic YAML gate — ONLY authorization path | engine.py (TOTAL: never raises, fail-closed, :1-21), config.py (strict load + sha256 version pin :197; failsafe blocks-all :201), history.py (stateful guards), audit.py (append-only writer :49), backtest.py | leaf | PRODUCTION-CRITICAL |
| razorpay | PaymentGateway adapter | client.py (raw REST httpx, Basic auth :62-68; retry only idempotent GETs :169; HMAC verify :143), factory.py (real vs simulator selection :24-35; get_real_gateway :72), simulated.py (twin), errors.py (typed errors) | leaf | PRODUCTION-CRITICAL |
| merchant | Real Razorpay sync (read side) | service.py (probe :191; run_sync :232; per-endpoint 4xx degradation :297-318; upsert on (source_type,external_id) :496-530; quarantine :532), client.py (GET-only collection client), normalize.py (strict validation → quarantine) | razorpay.errors only | PRODUCTION-CRITICAL |
| recovery | Closed-loop state machine | executor.py (state machine :1-17; FOR UPDATE lock :207; _gate :690; _fire :761; gateway-by-environment :180-195; UNKNOWN never blind-retry :824-838), builder.py (per-payment opportunities, dedup), strategies.py (6 candidates, confidence=evidence×fit), webhook_handlers.py (EVENT_HANDLERS :233; out-of-order-safe :18-22; amount cross-check hold :394-430), reconcile.py (ADR 0011 sweep) | policy, revenue, razorpay.errors | PRODUCTION-CRITICAL |
| detection | Anomaly detection over payment_events | engine.py (4 metrics, floors, dedup/merge, per-env :1-45), detectors.py (zscore/ewma/cusum/isolation_forest, baselines-first), series.py (bucketing, attempt-based metrics) | leaf (+schemas) | PRODUCTION-CRITICAL |
| diagnosis | ML root-cause classification | service.py (artifact pointer backend/artifacts/diagnosis_active.json; heuristic fallback flagged, :1-30), features.py, training.py, rescope.py (opt-in via DIAGNOSIS_WINDOW_RESCOPE), prodframe.py, taxonomy.py | detection (sanctioned) | PRODUCTION-CRITICAL |
| agent | AI investigator (advisory) | service.py (investigate flow :1-15; self-commit), tools.py (9-tool whitelist :295-675; AGENT_ACTOR "agent:investigator" :54), reasoners.py (heuristic default; LLM optional w/ validation+fallback), validation.py (hallucination guard), report.py | diagnosis, policy, revenue; NEVER razorpay (test_boundaries.py:78-82) | PRODUCTION-CRITICAL |
| revenue | Revenue-at-risk counterfactuals | engine.py (baseline counterfactual, read-only, :1-19), classify.py, statistics.py, config.py (documented priors) | leaf | PRODUCTION-CRITICAL |
| insights | Failure-facet outlier ranking | service.py (lift vs baseline, deterministic ranking, :1-21), facets.py, config.py | leaf | PRODUCTION-CRITICAL |
| worker | In-process scheduler tier | worker.py (3 units/tick: SCHEDULED retries, outbox, reconcile :103-138; per-row commits), supervisor.py (asyncio pacing via to_thread :69; liveness registry), senders.py (LoggingNotificationSender default — SIMULATED delivery; RazorpayNotesNotificationSender = provenance-tagged seam, no external delivery, :1-14) | recovery, policy | PRODUCTION-CRITICAL (opt-in) |
| audit | Chain verification | verify.py (full-table replay, env-unscoped by design :20-22; tamper-EVIDENCE not proof :24-27) | leaf | PRODUCTION-CRITICAL |
| evaluation | Experiment harness | runner.py (two arms in scratch SQLite DBs, baseline vs full loop, deterministic operator/customer roles, :1-31), holdout.py (sha256 customer holdout + Newcombe CI), export_training.py | EXEMPT (second composition root, test_boundaries.py:163-169) | RESEARCH-ONLY |

### 1.6 Simulator `simulator/` — RESEARCH-ONLY (demo-adjacent)

Deterministic synthetic payment environment: engine.py (single `random.Random(seed)`, ids derived from run id, :1-14), config.py (SCENARIOS), taxonomy.py/distributions.py (IST hourly weights, banks, failure classes), incidents.py (injected effects), cli.py (`run_idempotent` seeding used by demo router), `__main__.py`. Writes commerce rows stamped `source_type=simulator` + `simulator_ground_truth`. May NOT import services (test_boundaries.py:154-158). Reached in prod only via POST /api/v1/demo/* — never scheduled.

## 2. `backend/alembic` — migrations — SUPPORT (production-critical at boot)

4 linear revisions (ls alembic/versions): `77c0efef3d84` initial → `f3a9c1e7b204` provenance columns → `b4e7a1c2d305` environment core → `a83af82e8438` worker outbox + audit chain + hot-path indexes (HEAD, matches baseline:24). `env.py` relies on `app.models` import registering all tables (models/__init__.py:1-2). Applied at container boot (`alembic upgrade head && uvicorn`, Dockerfile.backend:22).

## 3. `backend/scripts` — operational entry points (one-way dep scripts→app; app never imports scripts — grep-verified, 52 vs 0 hits)

| Script | Entry | Purpose | Class |
|---|---|---|---|
| seed.py | :19 → simulator.cli.main | idempotent simulator dataset seed | SUPPORT (dev/ops) |
| simulate.py | :17 → cli.scenario_main | named scenario presets | DEMO-ONLY |
| demo_run.py (1077 ln) | :1038 | 5 deterministic in-process demo scenarios vs scratch SQLite | DEMO-ONLY |
| demo_live.py (386 ln) | :359 | 3 beats against the DEPLOYED stack (signed webhook, timeout→UNKNOWN, blocked refund) | DEMO-ONLY |
| export_openapi.py | :19 | writes contracts/openapi.json | SUPPORT (build tooling) |
| run_evaluation.py | :46 | baseline-vs-PulseRecover eval | RESEARCH-ONLY |
| run_multi_anchor.py (1431 ln) | :1101 | canonical spec across pinned date anchors → ml/experiments/multi_anchor/ | RESEARCH-ONLY |
| agent_eval.py (1856 ln) | :1824 | 7-metric agent scoring → ml/experiments/agent/ | RESEARCH-ONLY |
| train_models.py | :198 | temporal split, calibrated candidates, joblib → backend/artifacts/ (+ experiments row) | RESEARCH-ONLY bridge (trains the artifact prod reads) |

## 4. `backend/tests` — 131 test files — SUPPORT (quality gate)

- Root `conftest.py`: hermetic env pins BEFORE app import (:17-23 clears Razorpay keys, `API_KEY=dev-key`, `LLM_PROVIDER=none`); per-test in-memory SQLite via StaticPool (:36-53); per-test app+TestClient with get_db override (:56-65); factories make_merchant/make_payment/make_incident.
- 19 subsystem dirs: agent(9), agenteval(1), architecture(1 — import matrix enforcement), demo(1), detection(8), diagnosis(9), environment(8 — real/research isolation), evaluation(6), insights(5), integration(8), invariants(8 — safety), merchant(4), policy(5), provenance(1), razorpay(4), recovery(13 — largest), revenue(9), security(12 — audit chain, auth, prompt injection), simulator(7), worker(5).
- Root files pin: smoke (health, envelope, OpenAPI, API-key guard), real_data_workflow (mocked-httpx real flow), db_url normalization, export tz, health aggregation, model indexes.
- Gateway fakes live in subsystem conftests (15 files, ~2.2k lines).

## 5. `frontend/src` — Next.js 15 App Router — PRODUCTION-CRITICAL

All pages are client components; NO server-side fetching; no websockets — react-query polling only (providers.tsx QueryClient staleTime 10s, retry 1). `output:"standalone"` (next.config.ts:5).

### 5.1 Routes (9 pages)
`/` command center (dashboard summary 15s poll, timeseries 60s, top-5 opportunities, sync mutation); `/incidents` + `/incidents/[id]` (detail + investigation panel 2.5s poll while running); `/recovery` (queue 10s poll, approvals, plan/execute/approve/reject/escalate/cancel, backtest, reconcile); `/evaluation` → server redirect to `/research?tab=evaluation`; `/audit` (20s poll + verify action); `/payments`; `/research` (tabs: scenarios→DemoControl, evaluation→EvaluationView); `/settings` (merchant connection, sync toggle, webhook probe, ExportPanel).

### 5.2 Components (~40 feature + 7 ui primitives)
command-center, incident, recovery, evaluation (research-lab), audit, payments, research, settings, investigation (3-zone: OBSERVED FACTS / AI INFERENCE / RECOMMENDED ACTION), export, ui/ primitives; `demo-control.tsx` + `demo-run-summary.tsx` = DEMO-ONLY.

### 5.3 `lib/` — the API seam
- `api.ts` (407 ln, hand-written method per endpoint): base = `NEXT_PUBLIC_API_BASE_URL ?? http://localhost:8000` (:66-68); `X-API-Key` from `NEXT_PUBLIC_API_KEY` sent on EVERY request (:69,:143); 10s timeout / 120s for long-running (sync, build, reconcile, backtest, evaluation.run, demo trigger) (:70,:75); `cache:"no-store"`; envelope parsing (:181-205); blob for exports. NOTE: `api.detection.run` defined (:371-374) but has NO caller in src — client-side dead code (e2e uses it).
- `environment.ts`: two environments — `real_test` ("Real Merchant", DEFAULT :19) and `research` ("Research Lab"); transported as **query param only**, never a header; provider persists to localStorage; switcher in sidebar.

### 5.4 `frontend/scripts` + `frontend/e2e` — dev-tooling
`scripts/rz-discover.mjs` = uncommitted scratch Playwright probe (baseline:12). e2e: Playwright specs per page + global-setup seeding a scratch stack (ports 8001/3100); no unit/component tests exist (package.json scripts only lint/typecheck/test:e2e).

## 6. `contracts/` — SUPPORT

`openapi.json` (7056 lines, 42 paths) — generated by `scripts/export_openapi.py`, committed. architecture.md §9 claims "the frontend generates its client from it" — FALSE: frontend `lib/api.ts` is hand-written (frontend map §2); the contract is consumed by tests/docs only. UNCERTAIN whether any CI check enforces freshness.

## 7. `deploy/` + `render.yaml` + `Makefile` — SUPPORT (production-critical for the live system)

- `render.yaml`: 2 Docker web services, both free plan. `pulserecover-api`: healthCheck `/healthz`, env `APP_ENV=prod`, `SIMULATION_MODE=false`, `WORKER_ENABLED=true`, secrets sync:false. `pulserecover-web`: `NEXT_PUBLIC_API_BASE_URL` build-time inlined. Webhook setup comment lists events `payment.captured, payment.failed, order.paid, refund.processed, subscription.charged, subscription.charge_failed` — **omits `payment_link.paid`**, the only event type that verifies link-based recoveries (see architecture-actual.md §6 M7).
- `Dockerfile.backend`: python:3.12-slim, `alembic upgrade head && uvicorn ... ${PORT:-8000}` (migrate-on-boot, :22); policies/ copied into image (:18).
- `Dockerfile.frontend`: node:20-alpine 3-stage, NEXT_PUBLIC_* as build ARGs.
- `docker-compose.yml`: postgres:16 + backend (`APP_ENV=demo`, `SIMULATION_MODE=true` — judges get the SIMULATOR stack locally) + frontend.
- `Makefile`: setup/backend/test/migrate/export-openapi/compose-up (Windows Git-Bash paths).

## 8. `docs/` — 28 top-level docs + 11 ADRs + audit/

Claims corpus — verified elsewhere (architecture-actual.md §6 + docs-scout report). Categories: architecture-claims (architecture, data-flow, agent, detection, diagnosis/ml, evaluation, policy, recovery, worker, data-provenance, razorpay-integration, simulator, revenue-methodology, real-data-migration), runbooks (demo-chaos, demo-script, demo-rehearsal, demo, razorpay-integration), security (security-architecture, security-testing, payment-invariants, policy), process (decision-log D1-D19, product-strategy, competitive-analysis, index, ui-design-system), audit artifacts (claim-matrix, release-readiness). Known drift: security-architecture.md:34 claims APP_ENV excludes "prod" (false — config.py:30); real-data-verification.md is an unfilled template; ADR 0009 "no worker tier" superseded by worker.md (self-declared).

## 9. `ml/experiments` — RESEARCH-ONLY (record store)

160 files (89 json, 26 md, 17 py, 9 csv, 8 log, 6 joblib): canonical_spec.json (pinned eval contract: scenario standard, seed 42, end 2026-08-28, 65k events), agent/ (exp01-04), detection/ (4 exps + replay fixtures), diagnosis/ (dataset builder, baselines, calibration + joblib), multi_anchor/ (7 anchors + aggregate). **No code in backend/app reads ml/** — grep hits are docstring references only (detection/engine.py:136, diagnosis/prodframe.py:37). Production loads model artifacts from `backend/artifacts/` (diagnosis/training.py:546,592). Scripts WRITE here. Not a prod input.

## 10. `policies/` — PRODUCTION-CRITICAL

`default.yaml` (81 lines): kill_switch off; allowlist 8 actions (refund absent); auto_execute min_confidence 0.85 / max ₹5000 / max_attempts 2; require_human_approval ₹5000 / 0.85 (stricter-of-two rule, config.py:147-154); never_auto_execute: refund, irreversible_action, customer_opted_out; duplicate cooldown 60min; rate limits 10/incident, 3/customer/day, 100/global/hour; stopping rule 3+3. Loaded fail-closed; version pinned as `{version}+sha256.{12}` of file bytes (config.py:197).

## 11. Root files

`.env` (secrets, gitignored — never read), `.env.example`, `.gitignore`, `.dockerignore`, README.md, Makefile, render.yaml. DB files at backend/: pulserecover.db (local dev SQLite), e2e_test.db.

## 12. Cross-cutting who-calls-whom index (grep-verified)

- `run_detection` callers (app/): api/v1/detection.py:39, api/v1/demo.py:228, evaluation runner, artifacts/calib_detection.py. **No webhook→detection path; no scheduler runs detection.**
- `SyncService` callers: api/v1/merchant.py only (worker does NOT sync).
- `RecoveryExecutor` callers: api/v1/recovery.py, worker.py:147, reconcile.py:67, demo_live.py (script).
- `run_reconciliation` callers: api/v1/recovery.py:438 (POST /reconcile), worker.py:96 (cadence).
- `AgentService` callers: api/v1/agent.py. `DiagnosisService` callers: agent/service.py:203 (+ evaluation harness, scripts).
- `verify_chain` callers: api/v1/audit.py:80 (GET /audit/verify).
- `dispatch_event` callers: api/v1/webhooks.py:114, reconcile.py:107, evaluation harness.
- `PolicyEngine.evaluate` callers: executor._gate (:724), agent tools (propose/request), policy backtest.
- Frontend → backend: only via lib/api.ts (hand-written); environment as query param.

## 13. Classification ledger

- PRODUCTION-CRITICAL: backend/app (all of api, models, schemas, ports, config, db, ids, logging, main), services {policy, razorpay, merchant, recovery, detection, diagnosis, agent, revenue, insights, worker, audit}, policies/default.yaml, alembic, render.yaml, Dockerfiles, frontend/src.
- SUPPORT: scripts/{seed, export_openapi}, backend/tests, contracts/openapi.json, Makefile, docker-compose, .env.example.
- RESEARCH-ONLY: services/evaluation, scripts/{run_evaluation, run_multi_anchor, agent_eval, train_models}, ml/experiments, backend/artifacts (model store), frontend evaluation components.
- DEMO-ONLY: api/v1/demo.py (router), scripts/{demo_run, demo_live, simulate}, frontend demo-control/demo-run-summary, e2e seeds.
- DEAD/UNUSED: frontend `api.detection.run` binding (uncalled in src); `backend/e2e_test.db` (leftover file DB). Nothing else found — no orphan service packages (test_every_service_package_is_classified enforces, test_boundaries.py:264-279).

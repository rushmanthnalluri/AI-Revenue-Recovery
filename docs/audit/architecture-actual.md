# Architecture — ACTUAL (from code, not docs) — Audit Phase 2

Captured: 2026-09-02. Every edge cites path:line. `docs/architecture.md` / `docs/data-flow.md` are treated as CLAIMS; mismatches are registered in §6.
Status vocabulary per claim: WORKING / PARTIALLY_WORKING / BROKEN / MOCKED / SIMULATED / UNIMPLEMENTED / UNCERTAIN.

## 1. System context — Browser → Next.js → FastAPI → services → DB → Razorpay/Neon

```
+----------------+   REST /api/v1/* (fetch, X-API-Key on EVERY request,
|  Browser       |   react-query polling 2.5s-60s; no websockets, no SSR fetch)
|  Next.js :3000 |   environment = ?environment=real_test|research (query param)
+-------+--------+
        |  lib/api.ts:66-69,143 (NEXT_PUBLIC_API_BASE_URL / NEXT_PUBLIC_API_KEY build-time inlined)
        v
+-------+-------------------------------------------------------------+
| FastAPI :8000  app.main.create_app (backend/app/main.py:176)        |
| middlewares outer->inner: CORS -> RequestId -> AccessLog ->          |
|   RateLimit (in-mem, 120/60s webhooks, 60/60s mutating) -> ApiKey    |
|   (mutating /api/v1; 503 if unset; demo+detection exempt !=prod)     |
|   main.py:190-202, 85-116, 119-144                                   |
| routers (auto-discovered pkgutil, main.py:147-155) — COMMIT boundary |
|   -> services/* (flush-only; exceptions: reconcile/worker/agent)     |
|   -> models (24 tables, SQLAlchemy2, portable types ADR 0002)        |
| worker (opt-in WORKER_ENABLED, main.py:158-173; supervisor.py:62-79) |
+-------+----------------------------+--------------------------------+
        |                            |
        v                            v
+-------+--------+         +---------+---------------------------+
| DB             |         | Razorpay Test Mode (api.razorpay.com)|
| dev: SQLite    |         |  outbound: gateway writes + sync GETs|
|   (config.py:27)         |  inbound:  POST /webhooks/razorpay   |
| prod: Neon PG  |         |  (HMAC-SHA256, X-Razorpay-Signature) |
|   (render.yaml, baseline)|                                     |
| compose: pg16  |         +--------------------------------------+
+--------------=-+
```

Verified live state (baseline:31): healthz ok; system/health database ok (Neon), policy ok, gateway `razorpay_test`, worker ok/ticking; `APP_ENV=prod`, `SIMULATION_MODE=false`, `WORKER_ENABLED=true`.

## 2. Request path & router auto-discovery

`app.main._include_discovered_routers` (main.py:147-155) pkgutil-scans `app.api.v1` and includes any module-level `router` — 15 modules, 42 endpoints (inventory in repository-map.md §1.2; matches the 42-path OpenAPI export, baseline:50). WORKING.

Request lifecycle: CORS → request id stamped (`request_id_ctx`, main.py:55-64) → access log (:67-82) → rate limit (:85-116) → API key (:119-144) → router constructs service with request-scoped `Session` (db.get_db, db.py:98-104) → service flushes → **router commits** (e.g. merchant.py:81, detection.py:66, demo.py:272, webhooks.py:102,119) → uniform error envelope on failure (main.py:206-228). GET endpoints are unauthenticated by design; only mutating methods require the key (main.py:42,122).

## 3. Agent loop — agent → tools → policy → gateway → verification → audit

ACTUAL (verified end to end):

```
POST /api/v1/incidents/{id}/investigate (api/v1/agent.py:93)
  -> AgentService.investigate (agent/service.py:65)
       - returns latest completed report unless force_refresh (:70-73)
       - ensures diagnosis: DiagnosisService.classify if none (:189-210);
         diagnosis failure = recorded uncertainty, never aborts
       - EvidenceBundle (incident + diagnosis ctx) (:79-86)
       - reasoner = HeuristicReasoner default | LlmReasoner iff
         LLM_PROVIDER=openai AND OPENAI_API_KEY (:218-228, reasoners.py:1-16)
  -> reasoner may call ONLY the 9-tool AgentTools whitelist (tools.py):
       read:  get_incident(:295) get_payment_stats(:321) get_failure_distribution(:367)
              get_customer_history(:400) get_revenue_at_risk(:425) get_recovery_candidates(:463)
       dry-run: propose_recovery_strategy(:602) — policy PREVIEW, action_id=NULL
       mutate: request_payment_link(:659) request_recovery_execution(:675)
              -> creates PROPOSED recovery_actions row, amount copied from the
                 ORIGINAL payment/opportunity (never reasoner-supplied)
  -> LLM path only: JSON-schema validation + hallucination guard
       (unverifiable numeric claims stripped+flagged) -> heuristic fallback
       marked degraded (reasoners.py:8-13, validation.py)
  -> persist agent_reports row + audit_logs row; service self-commits
       (service.py:120,156 — documented exception to flush-only)
  [WORKING — heuristic; LLM path UNCERTAIN live: LLM_PROVIDER=none in prod (baseline:42)]

Execution of any proposal (agent- or human-initiated):
POST /api/v1/recovery/{id}/execute (api/v1/recovery.py:572)
  -> RecoveryExecutor.execute (executor.py:326)
       - SELECT ... FOR UPDATE on opportunity (:207-220; no-op on SQLite)
       - find-or-create open action (idempotent; IN_FLIGHT refuse :349;
         UNKNOWN -> resolve(), never re-fire :354-356)
       - _gate: PolicyEngine.evaluate(ActionContext) (:690-741) — EVERY
         execution, no exceptions; persists policy_decisions row
       - BLOCKED -> REJECTED | REQUIRES_APPROVAL -> PENDING_APPROVAL
         (approve/reject/escalate/cancel lanes :420-533)
       - ALLOWED/APPROVED -> _fire (:761): delayed-retry parks SCHEDULED;
         ESCALATE_HUMAN/NO_ACTION terminate gateway-free (:792-810)
       - _dispatch_gateway (:850): gateway chosen BY ENVIRONMENT
         (research -> injected/sim twin; real_test -> REAL adapter or honest
         razorpay_not_configured refusal, :180-195); only retry_payment
         (create_order), create_payment_link, notify_customer (outbox) have
         executor mappings (:867-928); other allowlisted types raise
         UNSUPPORTED_ACTION — see §6 M8
       - errors: 4xx -> FAILED (definitive); transient -> UNKNOWN (:813-838)
       - VERIFYING -> _verify_inline (simulator pays inline; real resolves
         via webhook) (:930-960)
  -> verification: webhook handlers mark RECOVERED/FAILED + audit row
       (webhook_handlers.py:154-237, _mark_action :332); UNKNOWN resolved
       GET-only via resolve()/reconcile sweep
  -> every transition appends hash-chained audit_logs row (executor.py:1123+,
       audit.py:49, models/system.py:79-117)
```

Policy gate (policy/engine.py): TOTAL (never raises — malformed input blocks, :104-178), deterministic, precedence BLOCKED > REQUIRES_APPROVAL > ALLOWED (:11-12); ALLOWED requires allowlist + no hard block + no stopping rule + no rate limit + no duplicate + amount <= ceiling + confidence >= floor + attempts < budget (:17-20); SAFE_ACTIONS (no_action, escalate_human) skip financial guards (:59); every decision persisted + BLOCKED additionally audited (:13-16). WORKING.

## 4. Razorpay path — sync + webhook → ingest → normalize → DB → analytics

### 4.1 Outbound (gateway writes) — WORKING (6 real payments per verified context)

`RazorpayGateway` (razorpay/client.py): raw httpx, HTTP Basic key_id:key_secret (:62-68); retry/backoff ONLY idempotent GETs (:169-190); mutating POSTs exactly once; receipt/reference_id carry `gateway_request_id` for dedupe (:87,:113); subscriptions have no dedupe field — ledger-only protection (:120-141). Factory: real keys present & !SIMULATION_MODE → real adapter, else simulator twin (factory.py:24-35); `get_real_gateway` never returns the twin (:72-84).

### 4.2 Inbound sync (merchant pull) — WORKING

`POST /api/v1/merchant/sync` (merchant.py:64) → `SyncService.run_sync` (merchant/service.py:232):
- refuses before any I/O when unconfigured or sync disabled (:252-266)
- auth canary `GET /v1/payments?count=1` (:285); windowed paged pulls orders/payments/subscriptions + targeted payment_links by OUR reference_ids (:290-295,:408-446)
- per-endpoint 4xx → entity degraded+quarantined, rest continues; all-4xx → run failed (:297-328) — this is the live 401-subscriptions behavior (baseline:40)
- upsert on `(source_type, external_id)` — zero duplicates on re-sync (:496-530); validation failures quarantined into entity_counts.errors (:532-551); service flushes, router commits (merchant.py:81)
- `source_type` derived from key prefix: rzp_test_* → `razorpay_test` → environment real_test (:106-133)

### 4.3 Inbound webhooks — WORKING (live-verified intake per baseline:41)

`POST /webhooks/razorpay` (api/v1/webhooks.py:60): 1 MiB cap → 413 before verification (:42-57) → HMAC-SHA256 over RAW body vs X-Razorpay-Signature, 400 on mismatch/missing (:66-73; client.verify_webhook_signature fails closed without secret, client.py:143-155) → require x-razorpay-event-id (:75-77) → persist raw WebhookEvent (source = razorpay|simulator by factory state, :91-99) → UNIQUE gateway_event_id dedupes retries → 200 already_processed zero side effects (:101-112) → `dispatch_event` → EVENT_HANDLERS: `payment.captured`, `payment.failed`, `payment_link.paid` ONLY (webhook_handlers.py:233-237); unregistered types stored + acked (:115-116) → handler failure keeps event processed=false for the sweep (:117-127) → out-of-order-safe payment state machine (captured terminal; late capture wins, :18-22,154-193) → payment_link.paid amount/currency/full-payment cross-check holds mismatches, never marks partial paid RECOVERED (:394-430) → linked actions transition + audit rows (:332-377). Router commits (:119).

### 4.4 Analytics consumption — PARTIALLY_WORKING (manual trigger)

Detection consumes the `payment_events` stream — written by the simulator (research) and by webhook transitions (`source="webhook"`, webhook_handlers.py:298-309) for real_test. **Detection runs only when triggered**: `POST /api/v1/detection/run` (detection.py:23), demo scenario anchored pass (demo.py:207-251), evaluation harness, or scripts. No webhook → detection cascade exists; the worker does not run detection (worker.py:103-138 ticks retries/outbox/reconcile only). Detection request defaults `environment="real_test"` (schemas/detection.py:45); a pass never sees the other environment's rows (engine via source_types_for_environment, engine.py:58). Incidents upsert per (metric, detector, window, segment); cross-window merge + post-resolve suppression (engine.py:1-45). Dashboard/incidents/insights/revenue are read-time over the same env-scoped rows.

## 5. Simulator path — synthetic → detect → diagnose → recover → evaluate

```
POST /api/v1/demo/scenario/{name} (demo.py:159)          [DEMO-ONLY]
  -> simulator.cli.run_idempotent: deterministic seed (random.Random(seed),
     run-derived ids, simulator/engine.py:1-14) -> commerce rows stamped
     source_type=simulator (-> research env) + simulator_ground_truth
  -> ONE anchored detection pass, pinned environment="research"
     (demo.py:213-228) -> incidents + evidence
POST /api/v1/demo/reset (demo.py:254): env-scoped deletes ONLY
     (research derived rows by environment col; simulator commerce by
     source_type; webhook_events where source='simulator'; keeps
     evaluation_runs/experiments/model_predictions/audit_logs; exactly one
     research-tagged demo.reset audit row) (demo.py:75-149,254-280)

Investigate/diagnose/recover on research rows use the SAME services as real:
  DiagnosisService (artifact backend/artifacts/diagnosis_active.json pointer;
  heuristic fallback flagged heuristic=true when absent, diagnosis/service.py:1-30)
  -> recovery build/plan/execute against the SimulatedPaymentGateway twin
     (executor._gateway_for research branch, executor.py:182-184)
  -> verification inline (twin pays synchronously, executor.py:930-960)
     or via simulated signed webhooks through the SAME intake

POST /api/v1/evaluation/run (evaluation.py:118)             [RESEARCH-ONLY]
  -> evaluation.runner: TWO scratch SQLite DBs (baseline arm = generic single
     retry for every failed payment; PulseRecover arm = full loop unchanged,
     verified through the real EVENT_HANDLERS registry), deterministic
     simulated operator + customer roles, sha256 customer holdout with
     Newcombe CI (runner.py:1-31, holdout.py:1-21) -> evaluation_runs rows
```
Simulator may not import services (test_boundaries.py:154-158). Evaluation is the exempt second composition root (:163-169).

## 6. DOCUMENTED vs ACTUAL — mismatch register

| # | Doc claim (evidence) | Actual (evidence) | Severity |
|---|---|---|---|
| M1 | architecture.md §1 module tree lists 9 service packages (docs/architecture.md:42-57) | 13 packages exist; tree OMITS `services.merchant`, `services.worker`, `services.audit` (ls app/services; test_boundaries.py:47-60 rules them) | MEDIUM — component map predates real-sync/worker waves |
| M2 | architecture.md §1: models = "21 tables" (docs/architecture.md:37) | 24 `__tablename__` in app/models (grep; list in repository-map §1.3) | LOW |
| M3 | architecture.md §4 dependency matrix "mirrored verbatim" from test (docs/architecture.md:166-186) | test RULES include merchant (may import razorpay), worker (recovery+policy), audit (leaf) — none appear in the doc table (test_boundaries.py:126-153) | MEDIUM |
| M4 | data-flow.md §8: "No background scheduler in v1" (docs/data-flow.md:121); reconcile.py docstring: "there is no background scheduler in v1; the worker tier is P2" (reconcile.py:24-25) | Worker tier shipped: worker.py (3 units/tick), supervisor.py; runs the sweep on cadence (worker.py:270-288); default off (config.py:56) but ENABLED in prod (render.yaml WORKER_ENABLED=true; baseline:31 worker ticking) | MEDIUM — stale operational claim in doc AND code comment |
| M5 | architecture.md §3 sequence: `WH->>DET: normalized payment_events` then the loop cascades automatically (docs/architecture.md:131-140) | No webhook→detection cascade exists. Detection triggers: POST /detection/run (detection.py:23), demo router (demo.py:228), evaluation harness, scripts only (grep-verified caller list, repository-map §12). Worker never detects (worker.py:103-138). The real_test closed loop does NOT close automatically | HIGH — core narrative overstates automation on the real path |
| M6 | architecture.md §8: system/health = "(DB, policy file, LLM provider, gateway mode)" (docs/architecture.md:263-264) | Also `worker` check with stale-tick degradation (health.py:63,85-105); aggregate status ok/degraded/error (:23-31) | LOW |
| M7 | render.yaml webhook setup comment: subscribe `payment.captured, payment.failed, order.paid, refund.processed, subscription.charged, subscription.charge_failed` (render.yaml:14-15) | EVENT_HANDLERS handles ONLY payment.captured / payment.failed / **payment_link.paid** (webhook_handlers.py:233-237). `payment_link.paid` — the ONLY event that verifies link-based recoveries (reference_id anchor, :196-230) — is absent from the documented subscription list; the 4 other listed types have no handlers (stored + acked "no handler registered", :115-116) | HIGH if the live subscription follows the comment — link recoveries would park VERIFYING indefinitely. UNCERTAIN: actual Razorpay dashboard config not observable from repo |
| M8 | policies/default.yaml allowlist includes extend_grace_period, pause_subscription, resume_subscription (policies/default.yaml:31-33); architecture.md §3 shows "subscription action" as executable (docs/architecture.md:148) | Executor maps ONLY retry_payment, create_payment_link, notify_customer; any other allowlisted type raises UNSUPPORTED_ACTION -> FAILED at fire time (executor.py:923-928). Subscription/grace actions are policy-ALLOWABLE but executor-UNIMPLEMENTED | MEDIUM — allowlist advertises actions the system cannot execute |
| M9 | architecture.md §9: "contracts/openapi.json … the frontend generates its client from it" (docs/architecture.md:278-279) | Frontend client is hand-written (frontend/src/lib/api.ts, 407 lines, one method per endpoint — no codegen artifact or script in package.json) | LOW |
| M10 | data-flow.md §1: detection request `{window, segment, detector, dry_run}` (docs/data-flow.md:9) | Schema field is `window_minutes` plus 16 more controls (noise floors, dedup/suppression windows, environment default `real_test`, baseline_mode, night floors) (schemas/detection.py:11-68) | LOW — simplification |
| M11 | security-architecture.md: APP_ENV is a Literal without "prod" — "prod is unreachable", exemptions unrelaxable (docs/security-architecture.md:34, per docs-scout) | `APP_ENV: Literal["dev","test","demo","prod"]` — prod IS reachable and DISABLES the exemptions (config.py:28-30, main.py:135); render.yaml sets APP_ENV=prod | MEDIUM — stale security reasoning; effective posture correct for the inverse reason |
| M12 | architecture.md §5.2: "the recovery agent builds a StrategyCandidate" (docs/architecture.md:202) | No "recovery agent" component exists; StrategyGenerator (recovery/strategies.py) builds candidates, RecoveryExecutor gates them. Only agent is the investigator | LOW — terminology |
| M13 | architecture.md §2: linear chain `API --> DET --> ML --> INV --> STR --> POL` (docs/architecture.md:97) | Actual composition: AgentService drives DiagnosisService (agent/service.py:203); StrategyGenerator is invoked by plan/execute, not after INV; detection is independent of investigation | LOW — diagram shorthand |
| M14 | data-flow.md §2 agent tools list (docs/data-flow.md:29-33) | MATCHES tools.py exactly (9 tools, tools.py:295-675) | — verified accurate |
| M15 | data-flow.md §6 webhook intake semantics (docs/data-flow.md:88-99) | MATCHES webhooks.py:42-119 + webhook_handlers.py exactly | — verified accurate |

## 7. Cross-cutting mechanisms (verified)

### 7.1 Router auto-discovery — WORKING
pkgutil scan of `app.api.v1`, includes module-level `router` objects (main.py:147-155). Adding a domain = adding one file; main.py never changes (docstring :3-5).

### 7.2 real_test/research isolation via source_type — WORKING
Commerce rows carry `source_type` (simulator|razorpay_test|razorpay_live) with NO environment column; environment derived via `source_types_for_environment` (models/base.py:41-70). Derived rows carry `environment` directly, default `research` (fail-safe: a forgotten stamp lands in the sandbox, base.py:73-88). Enforcement points: DetectionRunRequest environment literal (schemas/detection.py:45); demo reset env-scoped deletes (demo.py:117-149); executor gateway-by-environment with honest refusal (executor.py:180-195); webhook provenance stamped from factory state (webhook_handlers.py:269-286); sync source_type from key prefix (merchant/service.py:127-133). Frontend transports the choice as `?environment=` query param (frontend lib/api.ts query on every scoped call).

### 7.3 Services-flush / API-commits — WORKING with documented exceptions
Convention stated in executor.py:33-34, webhook_handlers.py:47-48, audit.py:18-20, merchant/service.py:19. Router commits verified: merchant.py:81,108; detection.py:66; demo.py:272; webhooks.py:102,119. Exceptions (all documented): reconcile per-unit commits (reconcile.py:16-22,82,111); worker per-row commits (worker.py:161,195); AgentService self-commit (agent/service.py:11,120,156); evaluation harness owns its scratch DBs.

### 7.4 Policy YAML sha256 pinning — WORKING
`load_policy_config` reads exact file bytes, strict-validates (extra=forbid models), sets `policy_version = "{version}+sha256.{first12}"` (policy/config.py:176-198). Every persisted decision carries it (engine.py:13-16; executor._gate details :734-739). Health endpoint echoes the live version (health.py:74-82). Fail-closed: unreadable/invalid file → PolicyConfigError; `failsafe_config` blocks everything (config.py:201-219).

### 7.5 Hash-chained audit — WORKING
Session-level `before_flush` hook stamps previous_hash/entry_hash on every new AuditLog (models/system.py:79-117) — transparent to all writers; sha256 over canonical fields (:24-40). `verify_chain` replays the whole table (created_at,id), flags edits/gaps/forks and post-genesis unhashed rows (audit/verify.py:48-78); served at GET /api/v1/audit/verify (api/v1/audit.py:80). Honest limits stated in code: tamper-EVIDENCE not proof; head deletion undetectable without external anchor; single-node assumption (verify.py:24-27, system.py:55-58). Environment-unscoped BY DESIGN (interleaved insertion order, verify.py:20-22) — matches frontend deliberately unscoped audit.verify call.

### 7.6 Opt-in worker — WORKING
`WORKER_ENABLED=false` default (config.py:53-56) so tests/scripts never spawn the loop; lifespan starts `start_worker` when true (main.py:158-173). Supervisor paces synchronous `Worker.tick` via `asyncio.to_thread` (supervisor.py:62-79) — slow ticks never block requests; liveness registry feeds system/health (health.py:85-105). Three failure-isolated units per tick: due SCHEDULED retries through the normal execute() path (same re-gate), notification outbox with linear backoff → FAILED after 3 attempts, reconcile sweep on cadence + first tick (worker.py:103-138,270-288). Single-process by design (worker.py:26-28). Notification delivery itself is SIMULATED: LoggingNotificationSender logs only; RazorpayNotesNotificationSender is a provenance-tagged seam with NO external delivery (senders.py:1-14) — customer comms never actually leave the system.

## 8. Deployment architecture (actual)

Render blueprint (render.yaml): two Docker web services, free plan (15-min idle spin-down, baseline:29-30). `pulserecover-api`: migrate-on-boot `alembic upgrade head && uvicorn … ${PORT:-8000}` (Dockerfile.backend:22), healthCheck `/healthz`, `APP_ENV=prod` (exemptions off), `SIMULATION_MODE=false`, `WORKER_ENABLED=true`, Neon Postgres DSN (scheme normalized to postgresql+psycopg:// at boot, db.py:46-56; 3s connect timeout, db.py:59-67), policies/ baked into image (Dockerfile.backend:18; config._resolve_path handles flattened /srv layout, policy/config.py:161-173). `pulserecover-web`: Next standalone (next.config.ts:5; Dockerfile.frontend 3-stage), NEXT_PUBLIC_* inlined at build time — **the API key ships to the browser** (frontend lib/api.ts:69; render.yaml:67-68); that is the only auth, a demo-grade posture acknowledged in code (deps.py:11-16). Compose stack is a DIFFERENT product posture: `APP_ENV=demo`, `SIMULATION_MODE=true` — local judges get the simulator (docker-compose.yml backend env). Razorpay webhook endpoint documented in render.yaml comments; the event-type gap there is M7 above.

## 9. What is NOT there (negative findings, grep-verified)

- No scheduler/queue other than the in-process worker (no Celery/RQ/cron imports in app).
- No authentication beyond the shared X-API-Key (no users/roles/sessions; KYA-lite is a cohort stamp, deps.py:32-117).
- No outbound customer comms (senders are logging/seam only, senders.py:1-14).
- No webhook→detection automation (M5).
- No production code reading `ml/` (docstring refs only; artifacts load from backend/artifacts/, diagnosis/training.py:546,592).
- No refund execution path (absent from allowlist AND from executor mappings — doubly unreachable: policies/default.yaml:22-35, executor.py:923-928).
- No LLM usage in prod config (LLM_PROVIDER=none, baseline:42) — heuristic reasoner is the live path.

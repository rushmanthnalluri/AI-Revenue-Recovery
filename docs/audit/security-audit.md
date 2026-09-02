# Security Audit — PulseRecover

Captured: 2026-09-02. Auditor: audit agent (Phase 11). Read-only review of repo @ dcef95a.
Status vocabulary: WORKING / PARTIALLY_WORKING / BROKEN / MOCKED / SIMULATED / UNIMPLEMENTED / UNCERTAIN.
Evidence grades: PROVEN-BY-TEST (a test exists that asserts the behavior) / PROVEN-LIVE / CODE-READ-ONLY / UNKNOWN.

## 1. Threat model

### 1.1 Actors
- **Merchant operator(s)** — share one static `X-API-Key` (config default `dev-key`, `backend/app/config.py:45`). No per-user authN/Z; KYA-lite maps keys to cohort principals (`backend/app/api/deps.py:47-58`). Self-declared `actor` strings on mutating calls.
- **Razorpay** — sends at-least-once, unordered webhooks (`backend/app/api/v1/webhooks.py:3-8`); trusted only via HMAC-SHA256 over the RAW body.
- **LLM (optional, OpenAI-compatible)** — untrusted reasoner; `LLM_PROVIDER=none` default → offline heuristic reasoner (`config.py:39`). LLM output treated as hostile: schema-validated, hallucination-guarded (`backend/app/services/agent/validation.py:1-8`).
- **Unauthenticated internet caller** — can reach all GET routes and the webhook endpoint; can attempt mutating routes (blocked without key).
- **DB-attacker** — considered in audit-chain honesty notes: full DB write access defeats tamper-evidence (`backend/app/services/audit/verify.py:24-28`).

### 1.2 Assets
- Razorpay key id/secret + webhook secret (env only, `config.py:33-35`); shared `API_KEY`; OpenAI key.
- Money movement: `recovery_actions` executions (orders/payment-links) — capped by policy gate, idempotency keys (`backend/app/services/recovery/executor.py:18-31`).
- Audit trail integrity (`audit_logs` hash chain).
- PII: customer name/email/phone (used in payment-link payloads, `executor.py:887-897`).
- Webhook event stream (raw payloads persisted in `webhook_events`).

### 1.3 Trust boundaries
1. Internet → FastAPI middleware chain: CORS → RequestId → AccessLog → RateLimit → ApiKey (`backend/app/main.py:190-202`; ordering comment `main.py:7-10`).
2. Razorpay → webhook ingress: body cap → HMAC verify → parse → dedupe (`webhooks.py:60-128`).
3. Reasoner (heuristic/LLM) → agent tool layer: whitelist-only dispatch, no getattr (`backend/app/services/agent/tools.py:17-19`, `tools.py:167-178`).
4. Everything → deterministic PolicyEngine before any gateway mutation (`executor.py:18-20`).
5. App → DB (SQLAlchemy ORM; raw SQL surface checked below).

## 2. Findings

### 2.1 Shared API-key auth — WORKING (PROVEN-BY-TEST), with a default-key caveat
- `ApiKeyMiddleware` guards mutating methods on `/api/v1` only (`main.py:42`, `main.py:119-144`). Constant-time compare via `hmac.compare_digest` (`main.py:137`).
- Fail-closed when `API_KEY` is empty: 503 `auth_not_configured` (`main.py:123-134`).
- Exemptions only when `APP_ENV != "prod"` for `/api/v1/demo` + `/api/v1/detection` (`main.py:44`, `main.py:135`). Live deployment runs `APP_ENV=prod` (docs/audit/baseline.md:31) so exemptions are off in prod.
- **Caveat (MEDIUM)**: `API_KEY` defaults to `"dev-key"` in code (`config.py:45`) — a deployment that forgets to set it has a publicly-known key in a public repo. Fail-closed only triggers on *empty*, not on *default*.
- Tests: `backend/tests/security/test_auth_boundaries.py` (see §2.x test map).

### 2.2 KYA-lite principal binding — WORKING as designed (demo-grade by design)
- `get_principal` maps authenticated keys to principal ids (`deps.py:106-117`); only the default `dev-key → demo-operator` mapping ships (`deps.py:47-50`), so in practice actor strings pass through unattributed (documented at `deps.py:41-46`).
- Key material never logged/persisted; only principal ids (`deps.py:38-39`).
- Honestly documented as "not SSO/OIDC/per-user authN/Z" (`deps.py:14-16`). This is attribution, not authentication.

### 2.3 Open GETs — by design, UNPROTECTED (PROVEN-LIVE)
- The middleware only guards MUTATING methods (`main.py:42`, `main.py:122`). ALL GET endpoints (dashboard, incidents, audit, agent reports, payments, policy…) are reachable with no credential. Live check below (§2.16).
- Implication: merchant payment data, customer PII, and audit trail are world-readable on the public deployment. Acceptable for a demo; a real deployment must not do this. (Severity tag in §3.)

### 2.4 Webhook HMAC fail-closed — WORKING (PROVEN-BY-TEST + PROVEN-LIVE)
- `verify_webhook_signature`: returns False when no secret configured or no signature — never "skip" (`backend/app/services/razorpay/client.py:143-155`); same for the simulator (`backend/app/services/razorpay/simulated.py:202-208`).
- Raw body verified BEFORE parsing; 1 MiB body cap enforced pre-verification (413) (`webhooks.py:40-57`, `webhooks.py:66-73`).
- Missing signature header → 400; mismatch → 400 (`webhooks.py:68-73`).
- PROVEN-LIVE: "webhook secret mismatch 400" incident (mission verified context; baseline.md:41 — live secret alignment fixed 2026-09-02).

### 2.5 Idempotency / replay
**Gateway side** — WORKING (PROVEN-BY-TEST):
- `gateway_request_id` unique per action, mapped to Razorpay `receipt` (orders) / `reference_id` (payment links) (`client.py:86-93`, `client.py:110-118`; model comment `executor.py:661-663`).
- Mutating POSTs sent exactly once — retries only for idempotent GETs on timeout/5xx/429 (`client.py:13-16`, `client.py:169-191`). Transient outcome → UNKNOWN, resolve by re-query (`executor.py:824-838`, `executor.py:533-609`).
- Subscriptions have no gateway-side idempotency — logged warning, notes-embedded ledger id (`client.py:120-141`).

**Duplicate-execute race** — WORKING on Postgres, WEAKER on SQLite (CODE-READ-ONLY):
- `SELECT ... FOR UPDATE` on the opportunity row serializes concurrent executors (`executor.py:207-220`); second executor reuses the open action or is refused (`executor.py:349-361`). Docstring notes SQLite silently omits the lock but writer-serialization orders the race (`executor.py:210-212`).
- UNCERTAIN: under SQLite, two concurrent `execute()` calls in separate processes — SQLite serializes writes, but the check-then-act between read of open action and insert relies on the transaction isolation; the unique constraint on `gateway_request_id` does NOT prevent two actions for one opportunity (each gets a fresh `gwr_` id). No DB-level unique constraint on (opportunity_id, open status) observed in executor; policy duplicate-protection guard is the backstop (`executor.py:22-25`). See policy duplicate guard below.

**Webhook dedupe** — WORKING (PROVEN-BY-TEST):
- `x-razorpay-event-id` → `webhook_events.gateway_event_id` UNIQUE; IntegrityError → 200 `already_processed`, zero side effects (`webhooks.py:100-112`).

### 2.6 LLM tool blast radius — SMALL, by construction (PROVEN-BY-TEST)
- 9 whitelisted tools; `call()` raises `ToolNotAllowed` otherwise — no arbitrary callables (`tools.py:150-178`).
- Only 2 mutation tools; both create PROPOSED actions with amount copied from the original payment/opportunity row — caller can never set the amount (`tools.py:11-16`, `tools.py:747-764`); they NEVER call the gateway (`tools.py:16`, `tools.py:749-752`).
- Confidence validated finite ∈ [0,1] before row creation — fail closed (`tools.py:101-116`).
- Execution still requires the executor's own policy gate + approval state (`tools.py:14-16`).
- Note cap: `note` truncated to 1024 chars (`tools.py:776`).

### 2.7 Prompt-injection defenses — WORKING, multi-layer (PROVEN-BY-TEST)
- LLM draft schema validation (pydantic, extra keys ignored, strict types) (`validation.py:27-71`).
- Hallucination guard: every numeric financial claim must exactly match a tool-result number, else stripped + report degraded (`validation.py:1-8`, `validation.py:173-197`, `validation.py:280-341`).
- Execution-advocacy language excised (`ADVOCACY_RE`, `validation.py:96-105`, `validation.py:264-278`).
- Fake evidence citations rejected: tool must be whitelisted AND called this run; evidence ids must be real (`validation.py:312-342`).
- Structured check: recommended action may only target ids a tool surfaced this run (`validation.py:377-391`).
- Confidence-vs-evidence-coverage degradation flag (`validation.py:393-404`).
- Tests: `backend/tests/security/test_prompt_injection.py`.

### 2.8 In-memory rate limiting — PARTIALLY_WORKING (by design single-node)
- Sliding window, per-process: webhooks 120/60s, mutating 60/60s, keyed by client host (`main.py:45`, `main.py:85-116`).
- Limits: per-process only (stated `main.py:86-87`) — multi-instance deploys multiply the limit; unauthenticated GETs are NOT rate limited (`_bucket` returns None for GETs, `main.py:93-99`); keying on `request.client.host` means one NAT'd cohort shares a bucket, and a distributed attacker bypasses it.
- 429 envelope: `rate_limited` (`main.py:111-114`).

### 2.9 CORS — WORKING, permissive methods/headers
- `allow_origins=settings.CORS_ORIGINS` (default `http://localhost:3000`, `config.py:48`), `allow_credentials=True`, `allow_methods=["*"]`, `allow_headers=["*"]` (`main.py:196-202`).
- Origins are an explicit list (no `*`), so credentialed cross-origin abuse is bounded to configured origins. Auth is a header (X-API-Key), not cookies, so CSRF surface is minimal.

### 2.10 Error-envelope leakage — WORKING (no internals leaked)
- Uniform envelope `{error: {code, message, request_id}}` (`main.py:48-52`).
- 500 handler: logs exception, returns generic "Internal server error." — comment "No stack traces or internals leak" (`main.py:224-228`).
- 422 handler: generic "Request validation failed." — pydantic detail NOT echoed (`main.py:215-222`).
- HTTPException handler echoes `exc.detail` as message (`main.py:206-213`) — route code controls these strings; check for sensitive detail in raises (spot-checked webhooks: generic messages only).


### 2.11 Hash-chained audit trail — WORKING as tamper-EVIDENCE (PROVEN-BY-TEST), limits honestly documented
- Session-level `before_flush` hook stamps `previous_hash`/`entry_hash` (sha256 over canonical fields) on every new `audit_logs` row (`backend/app/models/system.py:24-36`, `system.py:79-93`); whole pending batches chain in `(created_at, id)` order.
- `verify_chain` replays the full table, detecting: edited rows (at the row), recomputed-without-cascade edits and mid-chain deletions (at the successor), smuggled unhashed rows after genesis (`backend/app/services/audit/verify.py:48-78`); pre-chain NULL rows are legacy-valid.
- Verify endpoint `GET /api/v1/audit/verify` — itself an open GET.
- Documented limits (verified to match `docs/security-testing.md:177-187`): attacker with full DB write can recompute the whole chain (no external anchor); head-row deletion is silent; single-node/single-writer assumption (flush-time head query inside the writer's transaction — concurrent writers on separate connections could fork the chain).
- Tests: `test_audit_chain.py` (10 tests: mixed writers, determinism, tamper/forgery/deletion detection, legacy rows, post-genesis smuggle, endpoint).

### 2.12 PII — PRESENT, handled plainly (no masking), exposure bounded only by the open-GET posture
- Customer `name`/`email`/`phone` stored on `customers` and sent to Razorpay in payment-link payloads (`executor.py:883-897`).
- Read surfaces expose `customer_id` but not contact fields: `PaymentSummary` has no name/email/phone (`backend/app/schemas/payments.py:10-30`); agent `get_customer_history` returns counts + opted_out, no contact data (`tools.py:400-423`). No schema field carries email/phone (grep over `backend/app/schemas`: zero hits).
- So PII is (a) transmitted to the gateway by design and (b) not broadly re-exposed by the API — but the DB and webhook raw payloads (`webhook_events.payload`) hold whatever Razorpay sends, and the whole DB is reachable via open GETs only in aggregate. UNCERTAIN: no data-retention/deletion handling found; no encryption at rest beyond what Neon/Postgres provide.

### 2.13 Secrets handling / masking — WORKING (PROVEN-BY-TEST + live config check)
- Secrets arrive via env only; `.env` is gitignored (`.gitignore:19-21`), only `.env.example` (placeholders) is tracked (`git ls-files` shows `.env.example`, `frontend/.env.example` only).
- Structured logging redacts any key whose name matches secret hints (secret/key/token/password/authorization/signature/credential), recursively (`backend/app/logging.py:17-38`).
- Merchant layer exposes only masked key id (`••••` + last 4) (`backend/app/services/merchant/service.py:115-124`); key secret used only for HTTP Basic auth, never logged (`service.py:22-23`).
- Health endpoints reveal `APP_ENV`, `SIMULATION_MODE`, gateway mode, policy version, version (`health.py:39-71`) — config disclosure, not secrets; acceptable for demo, note for prod.
- Canary test seeds secrets through the whole flow and asserts they appear NOWHERE (responses, audit JSON, agent reports, webhook rows, logs) — `test_secret_leakage.py` (6 tests incl. `test_500_envelope_never_echoes_internals`).
- Accepted risk (documented `docs/security-testing.md:246-251`): simulator's default webhook secret ships in source — forgeable webhooks in SIMULATION_MODE only; real mode fails closed on empty secret (`client.py:150-151`).

### 2.14 SQL safety — WORKING (PROVEN-BY-TEST)
- No string-interpolated SQL found: grep for `sa.text(`/raw execute over `backend/app` shows only SQLAlchemy Core select expressions and fixed PRAGMAs (`db.py:83-84`, `simulator/cli.py:99-103`).
- Filters parameterized; SQLi shapes tested as inert (`test_input_abuse.py::test_opportunity_filters_are_parameterized`, `test_audit_filters_are_parameterized`, `test_null_byte_and_sql_in_incident_id_body`).
- `demo.py` reset builds DELETEs from a fixed `_RESET_TABLES` allowlist, not user input (`backend/app/api/v1/demo.py:254-258`).

### 2.15 Pydantic validation — WORKING (PROVEN-BY-TEST)
- Pagination bounded: `page >= 1`, `1 <= page_size <= 200` (`backend/app/schemas/common.py:23-25`); out-of-range → 422 (`test_input_abuse.py::test_out_of_range_pagination_is_422`); huge page → safe empty page.
- Wrong types on public POST bodies → 422 safe envelope (`test_input_abuse.py::test_recovery_bodies_reject_wrong_types`, `test_garbage_content_type_and_body`).
- Mutating recovery request schemas are thin by design — actor/note/reason/strategy_id only; NO amount or confidence fields exist on `ExecuteRequest`/`ApproveRequest` etc. (`backend/app/schemas/recovery.py:113-137`), so an API caller cannot inject money amounts at all (amounts come from DB rows).
- `APP_ENV` is a strict `Literal["dev","test","demo","prod"]` — exotic values rejected at startup (`config.py:30`; `test_auth_boundaries.py::TestAppEnvExemptionAbuse`).
- Gap noted: response/request `confidence` fields lack `ge=0, le=1` bounds at the schema layer (`schemas/recovery.py:24,48,99`) — enforcement happens in agent tools (`tools.py:101-116`) and the policy gate (`malformed.confidence`), not the schema. Low impact since API callers can't set confidence.

### 2.16 Live verification captures (2026-09-02, GETs only, no mutations against prod)
```
curl https://pulserecover-api.onrender.com/healthz                     -> 200
curl https://pulserecover-api.onrender.com/api/v1/system/health        -> 200
curl https://pulserecover-api.onrender.com/api/v1/payments?limit=1     -> 200 (no X-API-Key)
curl https://pulserecover-api.onrender.com/api/v1/audit?limit=1        -> 200 (no X-API-Key)
curl https://pulserecover-api.onrender.com/api/v1/dashboard/summary    -> 200 (no X-API-Key)
```
=> Open-GET posture PROVEN-LIVE on the public deployment. (Mutating-route behavior in prod not probed — mission restricts prod to open GETs.)

### 2.17 Test-evidence map (tests/security, 93 test functions across 10 files)
| File | Tests | Proves |
|---|---|---|
| test_auth_boundaries.py | 15 | every mutating route in the live route table 401s on missing/wrong/subtly-wrong key; empty-key fail-closed (503); APP_ENV Literal; prod disables demo/detection exemption; exempt routes structurally gateway-free |
| test_webhook_adversarial.py | 8 | concurrent duplicate deliveries (exactly one side effect); out-of-order link.paid recovered via reconcile; unknown payment stored not crashing; oversized body 413; deep-nest 400; NaN constants; non-object 400; binary garbage 400 |
| test_payment_link_verification.py | 11 | amount/currency/partial/missing-amount holds; corrected redelivery recovers; FAILED + late link-paid recovers; ack detail 200-char cap |
| test_gateway_inconsistency.py | 11 | resolve() identity-confusion guard; malformed 200s -> transient; httpx timeout configured; hanging gateway bounded; mutating call never retried; reconcile completes with hanging gateway |
| test_secret_leakage.py | 6 | canary secrets nowhere in any surface; redaction incl. nested; 500 envelope static |
| test_audit_chain.py | 10 | chain integrity + tamper/deletion/forgery detection + verify endpoint |
| test_input_abuse.py | 13 | 422 envelopes; pagination bounds; SQLi parameterized; unicode/null-byte ids; NaN/Inf confidence fail-closed; int64-max amount -> approval lane; arbitrary tool names refused |
| test_safety_invariants.py | 8 | stopping rule semantics; opt-out hard block (API + agent paths); low-confidence -> approval; ceiling cannot be bypassed |
| test_kya_separation_of_duties.py | 7 | same-cohort approval warning recorded, outcome-neutral |
| test_prompt_injection.py | 4 | injected data inert (heuristic); fake tool refused; refund/bypass advocacy contained; no inflation of well-behaved model |

Note: docs/security-testing.md:7 claims "107 tests" and :284 says "98 adversarial tests" in tests/security — actual count today is 93 test functions across the 10 files (grep `def test_` count). Doc-vs-repo mismatch (stale counts), worth flagging in synthesis. Per-file counts above from grep.

## 3. Decision-relevant findings summary (severity-tagged)

1. **[HIGH, by design, PROVEN-LIVE] All GET endpoints are world-readable** — merchant payments, incidents, audit trail, agent reports reachable with no credential on the public deployment (live probe §2.16; `main.py:122`). Fine for a demo; the single biggest gap for any real deployment.
2. **[MEDIUM, PROVEN-BY-TEST] `API_KEY` ships with a public default (`dev-key`)** in a public repo (`config.py:45`). Fail-closed covers empty-but-not-default. A prod deploy that forgets `API_KEY` is open to anyone who read the repo.
3. **[LOW] Single shared key = cohort identity, no per-user authZ** — acknowledged everywhere (`deps.py:14-16`); KYA-lite adds attribution + SoD warnings, not enforcement (`docs/security-testing.md:227-234`).
4. **[LOW, by design] In-memory, per-process rate limiting; GETs unlimited** (`main.py:85-116`). Multi-instance deploys multiply limits; unauthenticated read scraping unthrottled.
5. **[INFO-positive] Money-movement defenses are layered and test-proven**: policy gate on every execution, idempotency keys, no blind retries, UNKNOWN->re-query, row-lock on Postgres, webhook HMAC fail-closed, amount-verified link-paid recovery, tamper-evident audit chain (§2.4-2.7, 2.11). The financial core is the strongest part of the system.
6. **[INFO-positive] LLM blast radius is minimal even if an LLM is enabled**: whitelist tools, no gateway access, amounts from DB rows, hallucination guard, advocacy sanitizer, target-grounding (§2.6-2.7). Currently `LLM_PROVIDER=none` anyway.
7. **[LOW] Audit chain has no external anchor** — head deletion silent; single-writer assumption (documented honestly, §2.11).
8. **[LOW] Simulator default webhook secret is public** — forgeable sim webhooks; real mode fails closed (accepted risk #1, `docs/security-testing.md:246-251`).
9. **[INFO] Doc drift**: tests/security count in docs (107/98) vs actual 93 — docs stale, tests real.

## 4. Doc-vs-code claim verification (docs/security-testing.md)
- VULN-1..VULN-7 fixes: all verified present in current code (executor.py:566-569/584-586; main.py:123-134; webhooks.py:42-57/81-84; validation.py advocacy coverage incl. uncertainties/hypotheses; tools.py:101-116; webhook_handlers.py:394-430 + `_flag_verification_hold`; webhook_handlers.py:95-101 `_cap_detail`). MATCH.
- Audit-chain limits section: matches verify.py honesty notes. MATCH.
- Accepted risks 1-3, 5: match code. MATCH.
- Test counts (107/98): MISMATCH vs actual 93 (see §2.17).

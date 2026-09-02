# Flows A–C: Onboarding, Ingestion, Webhook — PulseRecover

Audit Phases 4A–4C. Captured 2026-09-02 by audit agent (functionality + flows A-C).
Evidence: file:line citations against HEAD `dcef95a`, plus live captures against `https://pulserecover-api.onrender.com` (Render free tier, cold starts).

## FLOW A — Merchant onboarding (credential model & connection probe)

**Status: WORKING (single-merchant, env-var onboarding only; no UI credential capture).**

### A.1 Credential model — env vars, single merchant, no onboarding API

- Credentials are **process environment only**: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `RAZORPAY_BASE_URL` (default `https://api.razorpay.com/v1`) — `backend/app/config.py:33-36`. Defaults are empty strings.
- There is **no endpoint, UI form, or DB table for submitting/rotating credentials**. Onboarding = set env vars + restart the process. Verified: grep for credential-writing routes finds only `GET /connection`, `POST /sync`, `POST /sync/enable|disable` in `backend/app/api/v1/merchant.py:56-128`; the frontend only *reads* connection state (`frontend/src/lib/api.ts:251-261`, `frontend/src/components/settings/settings-view.tsx:145` renders masked key id only).
- **Single-merchant by construction**: the connection cursor is a singleton row with hard-coded PK `'merchant'` (`backend/app/models/sync.py:24-25`, `CONNECTION_STATE_SINGLETON_ID`); `_ensure_merchant` anchors all synced commerce rows to one local `Merchant` per `source_type` (`backend/app/services/merchant/service.py:466-485`). No multi-tenant onboarding exists.
- Gateway selection: real keys present → real `RazorpayGateway`; missing keys or `SIMULATION_MODE=true` → simulator (`backend/app/services/razorpay/factory.py:24-35`, `60-69`). The simulator never silently replaces a configured real connection (factory.py:31-33).
- Secret hygiene (verified in code): only the **masked key id** is ever exposed — `mask_key_id` returns `rzp_test_••••<last4>` (`service.py:115-124`); the secret is used solely for HTTP Basic auth (`backend/app/services/merchant/client.py:60`, `backend/app/services/razorpay/client.py:64`) and never logged/returned (service.py:22-23).

### A.2 Connection probe

- `GET /api/v1/merchant/connection` (open GET, no API key needed — middleware only guards mutating `/api/v1` routes, `backend/app/main.py:122`) runs a **live probe** on every call: `SyncService.probe_connection()` (`service.py:191-205`) issues an authenticated `GET /v1/payments?count=1` (`backend/app/services/merchant/client.py:73-79`).
- Probe result is a typed structure: `ConnectionProbe(configured, connected, environment, key_id_masked, connection_error)` with `connection_error ∈ {None, 'authentication_failed', 'unreachable', 'gateway_error'}` (`service.py:94-103`).
- Response also reports `webhook_configured` (bool of `RAZORPAY_WEBHOOK_SECRET` presence, `backend/app/api/v1/merchant.py:47`), `sync_enabled`, `last_sync_at`, `last_webhook_at`, `last_sync_status` from the `connection_state` singleton (`merchant.py:39-53`).
- Environment is derived from the **key prefix**, not the URL: `rzp_test_*` → `test`, `rzp_live_*` → `live`, else `None` (`service.py:106-112`).

### A.3 Absent / invalid / unavailable behavior (all code-verified; live state confirms the happy path)

| Scenario | Code path | Observable behavior |
|---|---|---|
| Keys absent (or `SIMULATION_MODE=true`) | `service.py:152-155` (`_configured=False`), probe short-circuit `service.py:193-194` | `GET /connection` → `configured:false, connected:false`, all else null. `POST /sync` → `SyncNotConfiguredError` → **409** `razorpay_not_configured` (`service.py:252-256`, `merchant.py:77-78`) |
| Key format unrecognized (not `rzp_test_*/rzp_live_*`) | `service.py:257-261` | `POST /sync` → **409** `razorpay_not_configured` ("unrecognized Razorpay key format") |
| Keys present but wrong (401/403 from Razorpay) | `errors.py:117-118` maps 401/403 → `GatewayAuthenticationError`; `service.py:199-200` | `GET /connection` → `configured:true, connected:false, connection_error:"authentication_failed"`; `POST /sync` → auth canary fails the run (`service.py:280-285,329-334`), `sync_runs.status='failed'` with error recorded, HTTP 200 with the failed run row |
| Gateway unreachable / timeout / 5xx | `client.py:111-121` (3 attempts, exponential backoff 0.25·2^n) → `GatewayTransientError`; `service.py:201-202` | `GET /connection` → `connected:false, connection_error:"unreachable"`; `POST /sync` → run row `status='failed'`, error `GatewayTransientError: ...` |
| Other gateway error on probe | `service.py:203-204` | `connection_error:"gateway_error"` |
| Webhook secret absent | `merchant.py:47`; `client.py:150-151` (verify fails closed) | `webhook_configured:false`; every webhook delivery → 400 invalid signature (fail-closed) |

- **Live capture 2026-09-02 ~08:29 UTC** (Render, after 52s cold start on `/healthz`): `GET /api/v1/merchant/connection` → HTTP 200, `{"configured":true,"connected":true,"environment":"test","key_id_masked":"rzp_test_••••sMjo","webhook_configured":true,"sync_enabled":true,"last_sync_at":"2026-09-02T04:54:28Z","last_webhook_at":"2026-09-02T08:29:22Z","last_sync_status":"completed","connection_error":null}`. `GET /api/v1/system/health` → `app_env=prod`, `simulation_mode=false`, `gateway.status=ok detail=razorpay_test`, `database ok`, `policy_engine ok`, `llm_provider disabled/none`, `worker ok (last tick 11s ago)`.

### A.4 Onboarding gaps (factual, no UI onboarding)

- No self-serve onboarding: credential changes require a redeploy/env edit on Render; there is no rotation flow, no per-merchant isolation, no credential validation endpoint other than the implicit probe.
- `docs/*.md` claims of "Connect Razorpay" style onboarding should be read as: env-var configuration + this probe + the Disconnect/Reconnect toggle (`POST /sync/enable|disable`, `merchant.py:112-128`), which only gates sync — **inbound webhooks are unaffected by the toggle** (they authenticate by signature; `merchant.py:126-127`).

## FLOW B — Ingestion / sync (Razorpay Test Mode → DB)

**Status: WORKING (manual/API-triggered full sync; live run verified — see captures). One known product-level degradation: `GET /v1/subscriptions` and `POST /v1/payments` return 401 on the audit account (products not enabled), handled by per-entity degradation added in `dcef95a`.**

### B.1 Endpoints & auth

- `POST /api/v1/merchant/sync?window_days=N` (N ∈ 1..180, default 30 — `backend/app/api/v1/merchant.py:66`; clamp in `service.py:74-75,268`). Synchronous: the HTTP call blocks for the whole pass and returns the durable `sync_runs` row (`merchant.py:64-92`).
- Auth: `ApiKeyMiddleware` requires `X-API-Key` == `settings.API_KEY` (constant-time compare) for every mutating `/api/v1` route (`backend/app/main.py:119-144`). Sync is **not** in the demo-exempt prefix list (exempt = `/api/v1/demo`, `/api/v1/detection` only when `APP_ENV != prod`, `main.py:43-44,135`; live env is `prod`, so no exemptions at all).
- Refusals before any network/DB I/O: not configured → 409 `razorpay_not_configured`; sync disabled → 409 `sync_disabled` (`merchant.py:77-80`, `service.py:252-266`).
- Trigger surface: **manual only**. Grep confirms `run_sync` is called only from the API router (`backend/app/api/v1/merchant.py:71`); the in-process worker never calls it — there is no scheduled/automatic sync (worker code has no `SyncService` reference; `config.py:56-59` worker knobs are tick/reconcile only).
- Frontend: Settings page "Sync now" (`frontend/src/lib/api.ts:253-256`, `frontend/src/components/settings/settings-view.tsx:78`).

### B.2 What a pass pulls (service.py:280-328)

1. **Auth canary**: one authenticated `GET /v1/payments?count=1` (`client.py:73-79`). A 4xx here means the keys themselves are refused → the whole run is `failed` (comment `service.py:281-284`; caught by `except GatewayError` at `service.py:329-334`).
2. Merchant anchor: find-or-create one `Merchant` per `source_type` (`service.py:466-485`).
3. Window: `[now - window_days, now]` as unix `from`/`to` (`service.py:287-289`).
4. Four entity pulls, in order (`service.py:290-295`): `orders` → `payments` → `payment_links` → `subscriptions`. Orders first so payment→order FK resolution (`_local_order_id`, `service.py:487-494`) finds local orders.

### B.3 Pagination

- `from`/`to` (unix) + `count` (≤100, `MAX_PAGE_SIZE` `client.py:34-35`) + `skip`; stops on a short page; hard cap of 100 pages with a logged warning ("window is incomplete") (`service.py:355-369`).
- `GET /v1/payment_links` documents no pagination → fetched **only by targeted `reference_id`** of our own outbound recovery links (`service.py:408-446`; envelope may be a bare JSON array, accepted at `client.py:92-98`).

### B.4 Normalize / validate / quarantine

- `backend/app/services/merchant/normalize.py`: strict shape checks — required string `id` (:31-35), non-negative integer paise `amount` (bools/floats rejected, :38-43), `currency` (:55-59), documented status enums (`ORDER/PAYMENT/SUBSCRIPTION/PAYMENT_LINK_STATUSES`, :19-24, :46-52), unix `created_at` → tz-aware UTC (:62-66).
- Failures raise `EntityValidationError` → entity is **quarantined**: skipped and recorded under `sync_runs.entity_counts.errors` (max 50 entries, then `errors_truncated` counter — `service.py:79,532-551`). Never coerced, never crashes the run.
- Money stays integer paise end-to-end; the **raw upstream entity is preserved** under `meta["razorpay"]` on every synced row for reconciliation (`normalize.py:10-12,108-114,141-153,201-211`).
- Subscriptions carry no amount on the entity → stored `amount_paise=0` with `meta["amount_unknown"]=True` (`normalize.py:181-213`).
- Payment links have **no local table** by design: validated, counted, and their post-capture embedded `payments[]` are ingested as real payments (`service.py:408-446`, `normalize.py:162-178`).

### B.5 Upsert / provenance / dedupe

- Idempotent upsert keyed on **`(source_type, external_id)`** (`service.py:496-530`): re-syncs update in place — zero duplicates. `ingested_at` (first seen) and gateway `created_at`/`merchant_id` are immutable after insert (`service.py:504-507,525-527`).
- Provenance on every row: `source_system='razorpay'`, `source_type ∈ {razorpay_test, razorpay_live}` derived from key prefix (`service.py:127-133`), `external_id` = Razorpay id, raw snapshot in `meta.razorpay`.
- One `sync_runs` row per pass: `running → completed|failed`, with `entity_counts` JSON and `error` (`backend/app/models/sync.py:28-50`); `connection_state` singleton updated in the same flush (`last_sync_at`, `last_sync_status` — `service.py:335-339`); the API layer owns the commit (`merchant.py:81`).

### B.6 Retry & failure semantics

- HTTP layer: GET-only client, up to 3 attempts with exponential backoff (0.25·2^n s) on timeout/transport errors and 429/5xx (`client.py:111-136`). Safe because every call is an idempotent GET (`client.py:7-10`).
- Typed error mapping: 400→BadRequest, 401/403→Authentication, 404→NotFound, 429→RateLimit, 5xx→Server, malformed 2xx→Response (`backend/app/services/razorpay/errors.py:112-127`).
- **Per-entity degradation on 4xx (the `dcef95a` fix)**: a definitive per-endpoint refusal (`GatewayClientError`) during one entity pull quarantines that pull — `{"entity": kind, "id": None, "reason": "endpoint skipped: ... (GET /v1/<path> refused — is the product enabled on this Razorpay account?)"}` — and the rest of the catalog still syncs (`service.py:296-317`). The run is `failed` **only when every pull is refused** (`service.py:318-326`). This is exactly the audit account's state: Razorpay answers 401 on `GET /v1/subscriptions` while orders/payments/payment_links authenticate fine (comment `service.py:301-305`).
- Mid-run ambiguous failures (5xx/timeout after retries) → run `failed`, error on the row; partial upserts already flushed are kept — the next run reconciles idempotently (`service.py:247-250,329-334`).

### B.7 Sync enable/disable (Disconnect/Reconnect)

- `POST /api/v1/merchant/sync/enable|disable` flips `connection_state.sync_enabled` and writes an audited `merchant.sync_enable|disable` row (`merchant.py:95-128`). Disabled sync refuses with 409 **before any network I/O** (`service.py:20,263-266`). Webhooks unaffected (`merchant.py:126-127`).

## FLOW C — Webhook intake (`POST /webhooks/razorpay`)

**Status: WORKING (verified live: HMAC pass stores+processes; bad/missing signature → 400). At-least-once, unordered delivery handled by UNIQUE-event-id dedupe + an out-of-order-safe state machine.**

Pipeline order (all in `backend/app/api/v1/webhooks.py:60-128` unless noted):

### C.1 Raw body read, size-capped before any trust

- Body read with a hard cap of **1 MiB** (`MAX_WEBHOOK_BODY_BYTES`, `webhooks.py:39-42`), enforced both on the `Content-Length` header and while streaming (`webhooks.py:45-57`) → **413** beyond. The cap runs **before** signature verification so a junk flood cannot buffer unbounded bodies (`webhooks.py:39-41`).
- Route is outside `/api/v1`, so the API-key middleware does **not** apply (it only guards `path.startswith("/api/v1")`, `main.py:122`); instead webhooks get their own rate-limit bucket: **120 req/60 s per client IP**, in-memory sliding window (`main.py:45,85-116`). Per-process only (single-node assumption documented `main.py:86-87`).

### C.2 HMAC-SHA256 verification (raw body, fail-closed)

- `X-Razorpay-Signature` header required → missing = **400** (`webhooks.py:68-70`; live-verified, see captures).
- Verification is `HMAC-SHA256(webhook_secret, RAW body)` hex-compared with `hmac.compare_digest` against the header (`backend/app/services/razorpay/client.py:143-155`), invoked through the `PaymentGateway` port seam (`backend/app/api/deps.py:27-29`). The body is **never parsed before this check** (`webhooks.py:66-73`; client.py:146-147).
- **Fails closed when no secret is configured** (`client.py:150-151`) — a deployment without `RAZORPAY_WEBHOOK_SECRET` rejects every delivery with 400.
- Mismatch → **400** `Invalid webhook signature` + warning log (`webhooks.py:71-73`; live-verified).

### C.3 Event-id presence, then JSON parsing (post-signature only)

- `x-razorpay-event-id` header required → missing = **400** (`webhooks.py:75-77`).
- `json.loads` on the verified raw body: invalid JSON → **400**; `RecursionError` (pathological ~100k nesting) → **400**, deliberately never a 500 that would invite a retry storm (`webhooks.py:79-84`); non-object JSON → **400** (`webhooks.py:85-86`).

### C.4 Dedupe & raw persistence

- One `webhook_events` row per delivery: `gateway_event_id` (UNIQUE, indexed), `event_type`, full `payload` JSON, `signature_valid=True` (only verified deliveries are ever stored), `processed=False`, `received_at`, `source` = `razorpay|simulator` by `use_simulator(settings)` (`webhooks.py:91-99`; model `backend/app/models/system.py:120-136`).
- Duplicate `x-razorpay-event-id` → `IntegrityError` on commit → rollback + **200 ack `status:"already_processed", duplicate:true`, zero side effects** (`webhooks.py:101-112`). This is the at-least-once dedupe: Razorpay retries of the same event id are absorbed idempotently.

### C.5 Dispatch → handlers → state machine

- `dispatch_event(db, event_type, payload)` (`backend/app/services/recovery/webhook_handlers.py:104-127`) first stamps `connection_state.last_webhook_at` (real-gateway deployments only — simulator traffic never fakes real activity, `webhook_handlers.py:130-146`), then runs the registry:
  - `EVENT_HANDLERS` (`webhook_handlers.py:233-237`): `payment.captured`, `payment.failed`, `payment_link.paid`. **Only these three** — every other event type is stored and marked processed with the note "no handler registered" (`webhook_handlers.py:114-116`).
  - Handler contract: return `None` → `processed=True`; return a note → `processed=False` (reconcilable); raise → `dispatch_event` **rolls back the handler's partial writes**, keeps the stored event, marks `processed=False`, re-stamps webhook activity (`webhook_handlers.py:9-16,117-126`).
- Out-of-order safety (`webhook_handlers.py:18-22`):
  - `payment.captured` (:154-168): unknown payment → note, `processed=False`, "stored for reconciliation" (:159-160). Known payment → status transition + `PaymentEvent` audit row with provenance (:289-309); linked recovery actions in EXECUTING/VERIFYING/FAILED → **RECOVERED** (:165-167, `_OPEN_ACTION_STATES` :84-88).
  - `payment.failed` (:171-193): **captured is terminal** — a late `payment.failed` on a captured payment is a no-op (:178-180). Otherwise transition to failed, copy error code/description/source/method, mark linked actions FAILED (:181-193).
  - `payment_link.paid` (:196-230): resolves our outbound link by `reference_id == recovery_actions.gateway_request_id` (:208-217); idempotent on already-RECOVERED (:220-221); **financial-safety cross-check before RECOVERED** (`_link_paid_verification_hold` :394-430): integer `amount` must exactly equal `action.amount_paise` (missing/non-int → fail closed `amount_unverifiable`), `currency` must match, **partial payments never recover** (`partial_paid` status or `amount_paid < amount` → hold). A hold keeps the action open, sets `last_error`, and writes a `verification.amount_mismatch` audit row (`_flag_verification_hold` :433-475); the event itself is still marked processed (:36-40).
- State/audit writes: payment transitions append `payment_events` rows with webhook provenance (`source_type`/`source_system` stamped by actual gateway mode, `webhook_handlers.py:269-286`); action transitions write `audit_logs` rows `verify_recovered|verify_failed` with from/to status and trigger (:332-368). Hash-chained audit ledger: `backend/app/models/system.py:100-117`.
- Ack-detail hygiene: every `detail` string capped at 200 chars before it is echoed in the ack and stored on the event row (attacker-influenceable payload text; `webhook_handlers.py:90-101`). Ack target <5s is a documented goal (`webhooks.py:16`) — handlers are flush-only, the ingress commits twice (store, then processed stamp: `webhooks.py:100-119`).

### C.6 Replay / duplicate / malformed / ordering behavior — summary

| Case | Behavior | Evidence |
|---|---|---|
| Replay (same event id, valid signature) | 200 `already_processed`, `duplicate:true`, zero side effects | `webhooks.py:101-112` |
| Same payment event redelivered under a NEW event id | Handler idempotent: status checks (`!= "captured"`, `!= "failed"`) skip re-transition; action already RECOVERED → pass | `webhook_handlers.py:162-164,182-184,220-221` |
| Out-of-order (failed arrives after captured) | No-op — captured terminal | `webhook_handlers.py:178-180` |
| Out-of-order (captured arrives after failed) | Late success wins: payment→captured, FAILED action→RECOVERED | `webhook_handlers.py:19-22,162-167` |
| Webhook arrives before the payment was synced | `processed=False` "unknown payment; stored for reconciliation"; the reconcile sweep re-runs it through the same registry later | `webhook_handlers.py:159-160,176-177`; `backend/app/services/recovery/reconcile.py:96-115` |
| Malformed: no signature / bad signature / no event id / bad JSON / non-dict / >1 MiB | 400 / 400 / 400 / 400 / 400 / 413 — nothing stored | `webhooks.py:45-86` |
| Handler raises (bug/DB error) | Rollback of handler writes, event kept `processed=False`, ack 200 with capped detail | `webhook_handlers.py:117-126` |
| Unknown event type | Stored, `processed=True`, note "no handler registered" | `webhook_handlers.py:114-116` |

### C.7 Reconciliation of failed events

- `run_reconciliation` re-runs every `processed=false` event through the **same** `dispatch_event` registry (bit-for-bit identical to live intake), per-unit commits so one bad event cannot undo earlier repairs (`reconcile.py:16-22,96-115`). Triggered by `POST /api/v1/recovery/reconcile` (operator-triggered; `reconcile.py:24-25`) and by the worker on `WORKER_RECONCILE_SECONDS` cadence (`config.py:58-59`; worker tick verified live — system/health `worker ok, last tick 11s ago`).

## Live verification captures

All against `https://pulserecover-api.onrender.com` on 2026-09-02 ~08:29–08:33 UTC (curl; times include Render free-tier cold start). The audit-provided `X-API-Key` was used only for the sync POST; its value is deliberately not recorded here.

1. `GET /healthz` → **200** `{"status":"ok"}` (52.1s — cold start).
2. `GET /api/v1/system/health` → **200**: `app_env=prod`, `simulation_mode=false`, `checks.database ok`, `policy_engine ok (1.0+sha256.5a6afe61d6db)`, `llm_provider disabled (none)`, `gateway ok (razorpay_test)`, `worker ok (last tick 11s ago)`.
3. `GET /api/v1/merchant/connection` → **200**: `configured=true, connected=true, environment=test, key_id_masked="rzp_test_••••sMjo", webhook_configured=true, sync_enabled=true, last_sync_status=completed, connection_error=null`, `last_webhook_at` 7s before the probe (live webhook traffic arriving).
4. `POST /webhooks/razorpay` without signature header → **400** `Missing X-Razorpay-Signature header` (matches `webhooks.py:69-70`).
5. `POST /webhooks/razorpay` with `x-razorpay-signature: deadbeef` → **400** `Invalid webhook signature` (matches `webhooks.py:71-73`; nothing stored — rejection precedes persistence at `webhooks.py:91`).
6. `POST /api/v1/merchant/sync?window_days=30` with audit key → **200** in 7.7s: `status="completed"`, `orders {created:0, updated:6}`, `payments {created:0, updated:6}`, `payment_links {fetched:0}`, `subscriptions {created:0, updated:0}`, and exactly one quarantine entry: `{"entity":"subscription","id":null,"reason":"endpoint skipped: GatewayAuthenticationError: ... (GET /v1/subscriptions refused — is the product enabled on this Razorpay account?)"}`. This is the `dcef95a` per-entity degradation operating live: 6 real Razorpay Test Mode payments/orders re-upserted (all `updated`, none `created` — idempotent re-sync on `(source_type, external_id)`), subscriptions 401 quarantined, run still `completed`.
7. `POST /api/v1/merchant/sync` **without** `X-API-Key` → **401** `Missing or invalid X-API-Key header.` (matches `main.py:136-143`; live env is `prod` so no demo exemptions).

Not probed live (by design of the rules of engagement): `POST /sync/disable` (would flip shared state), webhook replay with valid signature (requires the webhook secret; verified instead by code + the audit-context note "webhook intake verified live (HMAC pass stored, bad signature 400)"), recovery execution.

## UNCERTAIN / unverifiable items

- **Webhook replay/duplicate ack (`already_processed`)**: code-verified (`webhooks.py:101-112`) but not live-probed — a replay test needs a validly-signed delivery, which requires the webhook secret (not available to the audit; sending junk would only re-verify the 400 path). The audit context independently records "webhook intake verified live (HMAC pass stored, bad signature 400)" on 2026-09-02.
- **413 body cap, RecursionError guard, non-dict 400**: code-verified (`webhooks.py:45-86`); not probed live (1 MiB junk flood against a shared free-tier deployment is poor citizenship; malformed-body probes would require valid signatures to reach the parse stage).
- **`POST /sync/enable` / `disable`**: code+test verified; not live-probed because flipping the shared connection state would disturb the live deployment mid-audit.
- **Worker reconcile actually repairing rows live**: the worker ticks (system/health) and the cadence code is test-covered, but whether any UNKNOWN action / failed webhook existed and was repaired in prod is not observable without DB access.
- **Ack <5s target** (`webhooks.py:16`): documented goal; live bad-signature rejects returned in <1s after warm-up, but the full handler path timing under load is UNCERTAIN.
- **`payments_observed: 0` explanation** (dashboard zeros despite 6 payments): code-grounded inference — dashboard/detection read `payment_events` (`series.py:167-190`) and sync upserts create no events; not directly confirmed by querying the prod DB.
- **Behavior when Razorpay is unreachable mid-sync** (run `failed`, partial upserts kept): code+test verified (`service.py:329-334`); not reproducible live without breaking the deployment.
- **`key_id_masked` live value** corroborates masking (`rzp_test_••••sMjo`); whether the last-4 suffix leaks anything sensitive: it is the public key id only, by design (`service.py:115-124`) — no secret material involved.

# Failure-Mode Audit — PulseRecover (20 mission cases)

Captured: 2026-09-02. Auditor: audit agent (Phase 12). Read-only review of repo @ dcef95a.
Evidence grades: PROVEN-BY-TEST / PROVEN-LIVE / CODE-READ-ONLY / UNKNOWN.
Companion: docs/audit/security-audit.md (threat model + control verification).

## Summary matrix

| # | Case | Behavior | Grade |
|---|------|----------|-------|
| 1 | Missing credentials | Refuse honestly, never fake (simulator or not-configured) | PROVEN-BY-TEST |
| 2 | Wrong credentials | Sync run fails at auth canary; probe reports `authentication_failed` | PROVEN-BY-TEST (unit) + PROVEN-LIVE (partial) |
| 3 | Gateway 401 (per-endpoint) | Degrade per entity; fail run only if ALL pulls refused | PROVEN-LIVE + PROVEN-BY-TEST |
| 4 | Gateway 429 | `GatewayRateLimitError`; GETs backoff-retried, mutations raised definitive | PROVEN-BY-TEST |
| 5 | Gateway 500 | Transient/ambiguous; GETs retried; mutation -> UNKNOWN, no blind retry | PROVEN-BY-TEST |
| 6 | Gateway timeout | 10s client timeout; same transient path as 5xx | PROVEN-BY-TEST |
| 7 | Bad webhook signature | 400 before parsing; fail-closed when secret unset | PROVEN-BY-TEST + PROVEN-LIVE |
| 8 | Duplicate webhook | UNIQUE event id -> 200 `already_processed`, zero side effects (race-safe) | PROVEN-BY-TEST |
| 9 | Out-of-order webhook | Captured terminal; late failed no-op; early link.paid reconciled later | PROVEN-BY-TEST |
| 10 | Malformed webhook | 400/413, never 500 (invalid JSON, deep nesting, binary, non-object) | PROVEN-BY-TEST |
| 11 | Cross-entity reference | Stored unprocessed + reconciled; resolve() requires id match | PROVEN-BY-TEST |
| 12 | Duplicate action | Row lock + open-action reuse + policy duplicate cooldown + unique ledger key | PROVEN-BY-TEST |
| 13 | DB down | Health reports `error`/database down; requests 500 with safe envelope | PROVEN-BY-TEST (health) / CODE-READ-ONLY (requests) |
| 14 | LLM down | Fall back to heuristic reasoner, report degraded | PROVEN-BY-TEST |
| 15 | Missing artifact | Deterministic heuristic diagnosis, flagged `heuristic=true` | PROVEN-BY-TEST |
| 16 | Low confidence | REQUIRES_APPROVAL lane; confidence caps; degraded flags | PROVEN-BY-TEST |
| 17 | Opt-out | Hard BLOCK, no approval lane, zero gateway calls | PROVEN-BY-TEST |
| 18 | Policy-exceeded | BLOCKED / approval lane; no path bypasses the gate | PROVEN-BY-TEST |
| 19 | Invalid policy file | Fail closed: loader raises; failsafe engine BLOCKs everything; health down | PROVEN-BY-TEST (loader/failsafe) / CODE-READ-ONLY (execute path) |
| 20 | Webhook handler exception | Rollback partial writes, keep event, `processed=False`, reconcile re-runs | PROVEN-BY-TEST |

## Case details

### 1. Missing credentials (Razorpay keys absent)
- **Expected/actual:** Simulator path serves the deployment honestly as simulation (`gateway_mode()` mirrors `get_gateway`, `backend/app/api/v1/health.py:53-54`). Real-mode-only operations REFUSE rather than fake: `RazorpayGateway.__init__` raises ValueError on empty key_id/secret (`backend/app/services/razorpay/client.py:56-57`); `RecoveryExecutor._gateway_for` raises `GatewayNotConfiguredError` (409 `razorpay_not_configured`) for a real_test opportunity without real keys — "NEVER a fake execution, NEVER the simulator for a real_test opportunity" (`backend/app/services/recovery/executor.py:142-148`, `executor.py:187-195`). Merchant sync raises `SyncNotConfiguredError` before any network I/O (`backend/app/services/merchant/service.py:86-87`, `service.py:252-253`).
- **Grade:** PROVEN-BY-TEST (recovery/gateway tests; `tests/security/test_auth_boundaries.py::test_demo_trigger_and_detection_run_cause_zero_gateway_mutations` covers adjacent seams). Live deployment HAS keys configured (baseline.md:31,40).

### 2. Wrong credentials (bad Razorpay keys)
- **Expected/actual:** Sync starts with an auth canary GET; a 4xx there means the KEYS are refused -> the whole run fails before pulling (`service.py:281-285`). `probe()` returns `ConnectionProbe(..., "authentication_failed")` with masked key id (`service.py:196-205`). 401/403 map to `GatewayAuthenticationError` (`backend/app/services/razorpay/errors.py:66-67`, `errors.py:117-118`).
- **Grade:** PROVEN-BY-TEST (tests/razorpay suite) for the mapping; CODE-READ-ONLY for the canary ordering. Related live signal: the audit account's keys ARE valid for orders/payments but refused on subscriptions/payments-POST (case 3) — proving key-level vs endpoint-level auth is distinguished in practice.

### 3. Gateway 401 on a specific endpoint (product not enabled)
- **Expected/actual:** Per-entity degradation: the refused endpoint's pull is quarantined into `entity_counts.errors` with an actionable message ("is the product enabled on this Razorpay account?"), the rest of the catalog still syncs; the run is `failed` only when EVERY pull is refused (`service.py:243-250`, `service.py:296-326`).
- **Grade:** PROVEN-LIVE — the real 2026-09 incident: GET /v1/subscriptions + POST /v1/payments 401 on the audit account (products not enabled); HEAD commit dcef95a is exactly this fix ("degrade per entity on 4xx endpoint refusals"); baseline.md:40. Also PROVEN-BY-TEST (merchant tests per commit message).

### 4. Gateway 429 (rate limited)
- **Expected/actual:** Mapped to `GatewayRateLimitError` — a CLIENT error (definitive rejection before processing), explicitly "the adapter itself never retries mutations" (`errors.py:18-19`, `errors.py:74-75`). For idempotent GETs the adapter backoff-retries transient statuses incl. 429 (max 3 attempts, exp backoff 0.25s base) (`client.py:169-191`). A mutation receiving 429 -> `GatewayClientError` -> action FAILED (definitive, nothing happened) (`executor.py:813-823`).
- **Grade:** PROVEN-BY-TEST (client retry tests; `tests/security/test_gateway_inconsistency.py::test_mutating_call_never_retried_on_timeout` covers the no-mutation-retry invariant; 429-specific retry asserted in tests/razorpay — UNCERTAIN on exact test name, mapping itself is errors.py:121-122).

### 5. Gateway 500 (server error)
- **Expected/actual:** `GatewayServerError` (subclass of `GatewayTransientError`) — outcome AMBIGUOUS (`errors.py:90-91`). GETs retried with backoff; a mutation gets exactly ONE attempt and the action transitions to UNKNOWN with `ambiguous_outcome: true` and resolution instructions — "NEVER blind-retry a mutating call" (`client.py:169-191`; `executor.py:824-838`). Recovery via `resolve()` re-query or the reconcile sweep (`backend/app/services/recovery/reconcile.py:69-94`).
- **Grade:** PROVEN-BY-TEST (`test_gateway_inconsistency.py::TestTimeoutsBounded`, reconcile-with-hanging-gateway tests).

### 6. Gateway timeout
- **Expected/actual:** Explicit 10s `httpx.Timeout` on the client (`client.py:65`; asserted by `test_httpx_client_has_explicit_timeout`). `httpx.TimeoutException`/`TransportError` -> bounded GET retries, then `GatewayTransientError` (`client.py:173-179`) -> mutation action UNKNOWN (same path as case 5). Hanging-gateway tests assert bounded behavior within timeout and that the reconcile sweep completes with zero mutations (`test_gateway_inconsistency.py:161-251`).
- **Grade:** PROVEN-BY-TEST.

### 7. Bad webhook signature
- **Expected/actual:** 400 `Invalid webhook signature` BEFORE any parsing; missing header -> 400; fails closed (rejects everything) when `RAZORPAY_WEBHOOK_SECRET` unset (`backend/app/api/v1/webhooks.py:68-73`; `client.py:150-155`). Body is verified RAW (never parsed/cast first). Constant-time compare.
- **Grade:** PROVEN-BY-TEST (webhook adversarial + secret canary tests) AND PROVEN-LIVE — the 2026-09-02 incident where a live secret mismatch produced 400s until alignment was fixed (baseline.md:41; mission verified context).

### 8. Duplicate webhook delivery
- **Expected/actual:** `x-razorpay-event-id` deduped against UNIQUE `webhook_events.gateway_event_id`; IntegrityError -> rollback, ack 200 `already_processed`, "zero side effects" (`webhooks.py:75-77`, `webhooks.py:100-112`). Concurrency race test: two threads, same event id, file-backed DB -> exactly one `received`, one `already_processed`, one stored event, one side effect (`tests/security/test_webhook_adversarial.py:65`).
- **Grade:** PROVEN-BY-TEST (incl. race).

### 9. Out-of-order webhook
- **Expected/actual:** Handlers are idempotent and out-of-order safe by contract (`webhook_handlers.py:18-22`): `payment.captured` is terminal — a late `payment.failed` is a no-op (`webhook_handlers.py:178-180`); a `payment_link.paid` arriving BEFORE the action row exists leaves the event stored unprocessed and the reconcile sweep recovers the action once it exists (`test_webhook_adversarial.py:177`); a late capture still moves a FAILED-linked action to RECOVERED (`webhook_handlers.py:165-167`).
- **Grade:** PROVEN-BY-TEST (`TestOutOfOrderDeliveries`).

### 10. Malformed webhook
- **Expected/actual:** Never a 500: >1 MiB body -> 413 (cap enforced pre-HMAC via Content-Length + streaming) (`webhooks.py:40-57`); invalid JSON -> 400; ~100k-deep nesting (RecursionError) -> 400; non-object JSON -> 400; valid-signature binary garbage -> 400 (`webhooks.py:79-86`). NaN/Infinity constants don't crash (`test_webhook_adversarial.py:285`).
- **Grade:** PROVEN-BY-TEST (`TestMalformedWebhookBodies`, VULN-3 fix).

### 11. Cross-entity reference (event for unknown payment; wrong-id gateway answers)
- **Expected/actual:** Webhook for an unknown payment id: stored, `processed=False`, acked 200, reconcilable later — never crashes (`webhook_handlers.py:159-160`, `webhook_handlers.py:176-177`; `test_webhook_adversarial.py:238`). UNKNOWN-resolution requires the re-queried entity id to MATCH the requested id; a mismatched answer is recorded as `*_id_mismatch` evidence and the action stays UNKNOWN (`executor.py:566-569`, `executor.py:584-586`).
- **Grade:** PROVEN-BY-TEST (`TestResolveIdentityConfusion` — VULN-1 regression; `test_event_for_unknown_payment_id_is_stored_not_crashing`).

### 12. Duplicate action / duplicate execute
- **Expected/actual:** Four layers: (a) `SELECT ... FOR UPDATE` on the opportunity row serializes concurrent executors on Postgres (`executor.py:207-220`); (b) second `execute()` reuses the open action; refuses while EXECUTING/VERIFYING or PENDING_APPROVAL; UNKNOWN -> resolve-by-requery instead of re-fire (`executor.py:349-361`); (c) policy rule `duplicate.cooldown` BLOCKs same customer+action-type inside the cooldown window (`backend/app/services/policy/engine.py:493-508`); (d) one gateway mutation per action ever — unique `gateway_request_id` mapped to Razorpay receipt/reference_id (`executor.py:18-25`, `executor.py:661-663`; `client.py:86-93`).
- **Caveat (CODE-READ-ONLY):** SQLite silently omits the row lock (`executor.py:210-212`); local demo relies on SQLite writer serialization. No DB unique constraint on (opportunity, open action) — the lock + reuse logic is the guard; production target is Postgres (Neon), where the lock holds.
- **Grade:** PROVEN-BY-TEST (policy + safety-invariant suites; concurrent duplicate-execute asserted at webhook level; executor-level double-fire covered by recovery invariants). Postgres-specific lock behavior CODE-READ-ONLY.

### 13. DB down
- **Expected/actual:** Health surfaces it honestly: `_db_check` reports `down` with the exception type name; top-level status becomes `error` when the database is down (`health.py:23-31`, `health.py:108-113`); readyz/system-health still answer HTTP 200 with the broken component flagged (`tests/test_health_aggregation.py:23-47`). Business requests: the `get_db` dependency fails -> unhandled exception -> 500 with the static safe envelope (no internals) (`main.py:224-228`).
- **Grade:** PROVEN-BY-TEST for health aggregation (mocked); CODE-READ-ONLY for request-path behavior (no live DB-kill test found).

### 14. LLM down
- **Expected/actual:** Default deployment has NO LLM (`LLM_PROVIDER=none` -> heuristic reasoner; `config.py:39`; live state baseline.md:42). If an OpenAI-compatible provider IS configured and fails: after max attempts the investigation falls back to the deterministic heuristic reasoner, marks the report degraded, and stamps `generated_by` with the fallback and `llm_error` in the raw payload (`backend/app/services/agent/reasoners.py:629-699`). Tool errors are fed back to the model as tool responses, not crashes (`reasoners.py:772-780`).
- **Grade:** PROVEN-BY-TEST (agenteval + prompt-injection suites exercise the fallback; `test_prompt_injection.py::test_model_following_injected_fake_tool_is_cut_off`).

### 15. Missing artifact (no trained diagnosis model)
- **Expected/actual:** `DiagnosisService.classify` loads the active artifact pointer (`backend/artifacts/diagnosis_active.json`); when none exists, a deterministic rule-based fallback labels the incident; the prediction row is flagged `heuristic=true`, model_name `diagnosis-heuristic`, explanation prefixed `[heuristic]` (`backend/app/services/diagnosis/service.py:1-16`). The agent report surfaces the fallback as an uncertainty (`reasoners.py:386-388`).
- **Grade:** PROVEN-BY-TEST (diagnosis suite); artifact presence in the live deployment UNCERTAIN (not probed — read-only mission).

### 16. Low confidence
- **Expected/actual:** Policy gate: confidence below `auto_execute.min_confidence` -> not auto-executable (approval lane at best) (`engine.py:17-20`); malformed/NaN confidence BLOCKed as `malformed.confidence` (fail closed; `tools.py:101-116` for the agent-tool surface). Agent layer: floor mirrored as `AUTO_EXECUTE_CONFIDENCE_FLOOR = 0.85` (`backend/app/services/agent/report.py:29`); non-auto-recoverable classes capped strictly below the floor (`report.py:31-33`); confidence at the floor with failed evidence coverage flagged degraded (`backend/app/services/agent/validation.py:393-404`); escalation threshold routes low-confidence investigations to humans (`report.py:167` ESCALATION_CONFIDENCE_THRESHOLD).
- **Grade:** PROVEN-BY-TEST (`test_safety_invariants.py::test_agent_tool_path_routes_to_approval`, agenteval low-confidence cases — docs/security-testing.md attack row 10).

### 17. Opt-out
- **Expected/actual:** Hard block: policy rule `never_auto_execute.customer_opted_out` fires `_BLOCK` — "no automated recovery contact is permitted" — with NO approval lane (`engine.py:359-363`); verified for every action type via both the API path and the agent tool path; zero gateway calls; block mirrored to audit (`tests/security/test_safety_invariants.py:144`, `:176`). The customer opt-out flag flows into the gate via `ActionContext.customer_opted_out` (`executor.py:706-723`; `tools.py:587-600`).
- **Grade:** PROVEN-BY-TEST.

### 18. Policy-exceeded (amount ceiling / attempts budget / rate limits / stopping rules)
- **Expected/actual:** Deterministic gate, precedence BLOCKED > REQUIRES_APPROVAL > ALLOWED (`engine.py:12-20`): amount above ceiling -> approval lane; attempts budget exhausted -> approval lane with explicit reason (`engine.py:392-396`); per-customer daily rate limit -> BLOCK (`engine.py:487-491`); global hourly budget -> BLOCK (`engine.py:510-512`); consecutive-failure stopping rule per incident -> BLOCK (re-armed only by a success) (`test_safety_invariants.py:55-110`). ₹10,000 (int64-max) takes the approval lane; direct-strategy execute cannot bypass the ceiling (`test_safety_invariants.py:169`, `:257`). Human approval lane then fires exactly once (`test_safety_invariants.py:215`).
- **Grade:** PROVEN-BY-TEST.

### 19. Invalid policy file
- **Expected/actual:** Fail closed at every layer: `load_policy_config` raises `PolicyConfigError` on unreadable/unparseable/invalid files (`backend/app/services/policy/config.py:176-198`; 12 negative tests in `tests/policy/test_config_loader.py`); `PolicyEngine.from_file` propagates it — "fail closed" (`engine.py:220-229`); `PolicyEngine.failsafe` BLOCKs everything with version "failsafe" (`engine.py:232-235`; `test_failsafe_engine_blocks_everything`); AgentTools catches load failure and binds the failsafe engine (`backend/app/services/agent/tools.py:138-143`); health reports `policy_engine: down` (`health.py:74-82`; `test_health_aggregation.py:50-57`).
- **Hole (CODE-READ-ONLY):** the recovery executor constructs `PolicyEngine.from_file(...)` with no catch (`executor.py:173`) — on a broken policy file an execute/approve request raises `PolicyConfigError`, which is NOT a `RecoveryError`, so the API layer's domain-error mapping misses it and the request 500s with the safe envelope (`main.py:224-228`). Net effect is still fail-closed (nothing executes), but the surface is an opaque 500 rather than a clean 4xx/409. UNCERTAIN whether any test covers the execute path with a broken policy file (grep found loader/engine/failsafe tests only).

### 20. Webhook handler exception mid-processing
- **Expected/actual:** `dispatch_event` rolls back the handler's partial writes, keeps the stored event, re-stamps webhook activity, returns `processed=False` + a 200-char-capped `handler error: <type>: <msg>` detail — the event stays reconcilable and the sweep re-runs it through the same registry (`webhook_handlers.py:104-127`; `reconcile.py:96-138` per-unit commits so one bad event can't undo other repairs). Detail text capped to bound attacker-influenced reflection (`webhook_handlers.py:90-101`).
- **Grade:** PROVEN-BY-TEST (`test_payment_link_verification.py::test_handler_error_detail_capped`; reconcile sweep tests; out-of-order recovery test).

## Cross-cutting observations
- The system's failure philosophy is consistent and evidence-backed: **definitive failures fail loudly and truthfully; ambiguous outcomes go UNKNOWN and are resolved by re-query, never by blind retry** (errors.py:9-20; executor.py:26-31).
- Every failure path that touches money appends audit rows (hash-chained), including holds and still-unknown checks (`executor.py:595-609`; `webhook_handlers.py:433-475`).
- Weakest-covered cases: DB-down request path (no live test), Postgres row-lock behavior (SQLite test env), execute-with-broken-policy 500 (case 19 hole), 429-specific retry assertion (mapping tested; retry timing assumed from client tests).

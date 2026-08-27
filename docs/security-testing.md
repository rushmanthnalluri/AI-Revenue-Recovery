# PulseRecover — Adversarial Security Testing

Companion to `docs/security-architecture.md` (design posture) and `docs/policy.md`
(threat model). This document records a dedicated break-it engagement: 13 attack
vectors aimed at the **untested** surface, what broke, the fixes, and the proof.

> Suite: `backend/tests/security/` (77 tests). Every fix below has a regression
> test there; every "safe" verdict has a proof test, not an assertion of faith.

## Mandatory guarantees — verdict

| Guarantee | Verdict | Where proven |
|---|---|---|
| No secret leakage | **HOLDS** (proof) | `test_secret_leakage.py` canary sweep |
| No unrestricted LLM execution | **HOLDS** (proof) | `test_prompt_injection.py`, agenteval |
| No duplicate financial action | **HOLDS** (incl. concurrent) | `test_webhook_adversarial.py` race test |
| No unverified webhook acceptance | **HOLDS** (+ hardened) | `test_webhook_adversarial.py` |
| No action beyond policy limits | **HOLDS** | `test_safety_invariants.py` |
| Safe stopping | **HOLDS** (semantics match docs) | `test_safety_invariants.py` |
| Human escalation when required | **HOLDS** | injection suite + agenteval regression |
| Complete audit trail | **HOLDS** | block-mirror + reset + reconcile asserts |

## Vulnerabilities found and fixed

### VULN-1 — Identity confusion in UNKNOWN resolution (CONFIRMED, fixed)

- **What:** `RecoveryExecutor.resolve()` re-queried gateway truth for an UNKNOWN
  action but trusted the *status* of the response without verifying the returned
  entity id matched the requested id. A `fetch_payment("pay_A")` answering with a
  captured payload for `pay_B` moved the action to **RECOVERED** — false
  recovered revenue, breaking the "verification proves" guarantee.
- **Attack:** `ConfusedGateway` double whose `fetch_*` always answer captured/paid
  for a *different* id than requested.
- **Fix:** `app/services/recovery/executor.py` — both resolve paths now require
  `response.id == requested_id` before trusting the status; a mismatch is
  recorded as `order_id_mismatch` / `linked_payment_id_mismatch` evidence in the
  `recovery.action.resolve_check` audit row and the action stays UNKNOWN.
- **Regression:** `tests/security/test_gateway_inconsistency.py::TestResolveIdentityConfusion`
  (wrong-id on both paths stays UNKNOWN + auditable; matching-id still recovers).

### VULN-2 — Empty `API_KEY` failed OPEN (CONFIRMED, fixed)

- **What:** the API-key middleware compared the header (default `""`) with the
  configured key via `hmac.compare_digest`. An operator setting `API_KEY=""`
  made every mutating `/api/v1` route unauthenticated (`"" == ""`).
- **Attack:** monkeypatched `settings.API_KEY = ""`, POSTed a mutating route
  with no header → request passed the gate (404 from the route, not 401).
- **Fix:** `app/main.py` — the middleware now fails **closed** when no key is
  configured: mutating requests get `503 auth_not_configured` (GETs stay open).
- **Regression:** `tests/security/test_auth_boundaries.py::TestApiKeyFailClosed`.

### VULN-3 — Webhook intake resource exhaustion (CONFIRMED, fixed)

- **What:** the webhook route read the request body unboundedly into memory
  (a 10MB+ junk flood is buffered in full), and `json.loads` at ~100k nesting
  depth raises `RecursionError`, which escaped the `except ValueError` and
  surfaced as a **500** (inviting a Razorpay retry storm).
- **Fix:** `app/api/v1/webhooks.py` — hard 1 MiB body cap (`413 payload_too_large`,
  enforced before HMAC via Content-Length/streaming), and `RecursionError` now
  maps to a clean 400.
- **Regression:** `tests/security/test_webhook_adversarial.py::TestMalformedWebhookBodies`.

### VULN-4 — Advocacy-language sanitizer gap in LLM output (CONFIRMED, fixed)

- **What:** the execution-advocacy sanitizer ("skip approval", "bypass the
  policy gate", …) was applied only to `what_happened` and the recommendation
  rationale. The same language in `uncertainties`, fact statements, inference
  statements, or hypothesis causes landed verbatim in the persisted report —
  an operator-manipulation surface (advisory only; no execution path existed).
- **Fix:** `app/services/agent/validation.py` — `sanitize_advocacy` now covers
  all free-text draft fields.
- **Regression:** `tests/security/test_prompt_injection.py` (asserts the live
  report fields carry none of the injected advocacy).

### VULN-5 — NaN/Infinity confidence crashed the agent mutation tools (CONFIRMED, fixed)

- **What:** Python's `json` accepts `NaN`/`Infinity`, so an LLM tool call can
  carry them. `AgentTools._request_action` wrote the row *before* the policy
  gate ran; SQLite binds NaN as NULL → `IntegrityError` on the NOT NULL
  `confidence` column. The gate's `malformed.confidence` block never fired —
  the investigation crashed instead of failing closed.
- **Fix:** `app/services/agent/tools.py` — mutation tools validate confidence
  (finite, in `[0,1]`) **before** any row exists and raise `ToolError`
  (fed back to the LLM as a tool error). The dry-run `propose_recovery_strategy`
  still passes NaN to the gate, which BLOCKs it — both surfaces fail closed.
- **Regression:** `tests/security/test_input_abuse.py::TestExtremeNumericInputs`.

## Attack matrix

| # | Vector | Method | Result | Proof test |
|---|---|---|---|---|
| 1 | Inventory of privileged routes | Enumerated the live route table (unwraps `_IncludedRouter`); every mutating `/api/v1` route fuzzed for missing/wrong/subtly-wrong `X-API-Key` | SAFE — all 13 mutating routes 401; audit coverage confirmed on recovery/demo/agent paths | `test_auth_boundaries.py::TestRouteTableAuthFuzz` |
| 2 | Unauthorized financial action | Above + `reconcile`/`opportunities/build` explicitly; demo/detection exemption probed for financial effect (structural: no gateway dependency in the exempt dependant tree; behavioral: full trigger with counting gateway) | SAFE for exemption; **VULN-2** for empty key (fixed) | `TestDemoExemptionHasNoFinancialEffect`, `TestApiKeyFailClosed` |
| 2b | `APP_ENV` abuse | `PROD`, `Prod`, `production`, whitespace, newline payloads | SAFE — pydantic `Literal` rejects all at startup (fail closed); `"prod"` unreachable | `TestAppEnvExemptionAbuse` |
| 3 | Prompt injection **via data** | Incident title/description, customer name, `error_description`, `meta.error_reason` seeded with: "ignore previous instructions", fake tool-call JSON, a fake `execute_refund_now` tool, invented ₹ amounts, approval-bypass text. Heuristic path + scripted `chat_fn` that *follows* the injection | SAFE by construction (heuristic: data inert, whitelist-only tools, zero action rows); LLM path: fake tool refused → fallback; refund → policy BLOCKED/REJECTED; invented money + advocacy stripped; 0.99 capped to evidence ceiling; **VULN-4** sanitizer gap fixed | `test_prompt_injection.py` (5 tests) |
| 4 | Malformed inputs | 10MB webhook body; 100k-deep JSON; NaN/Infinity in signed payloads; binary garbage; wrong types on every public POST body; `page/page_size` 0/−1/10⁹; SQLi shapes in filters; emoji/unicode/null-byte ids; NaN/∞ confidence via tool JSON; 2⁶³−1 amount | SAFE except **VULN-3** (fixed) and **VULN-5** (fixed); SQLi fully parameterized (no bypass, tables intact); 2⁶³−1 takes approval lane | `test_input_abuse.py`, `TestMalformedWebhookBodies` |
| 5 | Timeouts beyond execute | httpx client timeout asserted configured; hanging transport → bounded retries (exactly 3 GET attempts, 1 POST attempt — no blind mutation retry); reconcile sweep over 3 UNKNOWN actions with a hanging gateway completes, all stay UNKNOWN, zero mutations | SAFE — bounded everywhere | `test_gateway_inconsistency.py::TestTimeoutsBounded` |
| 6 | Concurrent duplicate webhooks | Two threads racing the same `x-razorpay-event-id` on a file-backed DB with independent sessions | SAFE — exactly one `received`, one `already_processed`, one stored event, one capture side effect | `TestConcurrentDuplicateDeliveries` |
| 7 | Delayed/out-of-order webhooks | `payment_link.paid` before the action row exists; events for unknown payment ids | SAFE — stored unprocessed (acked 200), reconcile sweep recovers the action once it exists | `TestOutOfOrderDeliveries` |
| 8 | Inconsistent gateway responses | Wrong-id `fetch_payment`/`fetch_order`; 200-with-error-envelope; non-JSON 200; 200 missing `id` | **VULN-1** (fixed); malformed 200s already map to transient/ambiguous | `TestResolveIdentityConfusion`, `TestMalformedGatewayResponses` |
| 9 | Repeated failures / stopping rule | 3 FAILED across 3 strategies → 4th blocked (incident rule); RECOVERED in between resets the streak; 3 fresh failures re-arm it | SAFE — behavior matches `docs/policy.md` R04/R05 | `TestStoppingRuleSemantics` |
| 10 | Low-confidence diagnoses | agenteval suite re-run after the validation fix (0.84 cap + escalation floors) | SAFE — 8/8 agenteval green | `tests/agenteval` (regression) |
| 11 | Excessive amounts | ₹10,000 via agent tool, via direct strategy execute, via API execute | SAFE — all paths land PENDING_APPROVAL, zero autonomous gateway calls; human lane fires exactly once | `TestExcessiveAmounts` |
| 12 | Unsafe AI recommendations | agenteval adversarial cases + new injection-via-data vectors (3) | SAFE | `test_prompt_injection.py`, agenteval |
| 13 | Customer opt-out | opted-out customer × {retry, payment link, notify} via API path AND agent tool path | SAFE — hard BLOCKED (no approval lane), zero gateway calls, block mirrored to audit | `TestCustomerOptOut` |
| S | Secret-leakage sweep | Canaries in `RAZORPAY_KEY_SECRET` / webhook secret / API key / OpenAI key through a full seeded flow (bad sig, good sig, 401s, execute-vs-401-gateway, investigate); redaction unit tests; 500-envelope test | SAFE — canary appears nowhere: responses, audit JSON, agent reports, webhook_events, `last_error`, structured logs; authorization/secret-shaped keys redacted incl. nested; 500 envelope static | `test_secret_leakage.py` |

## Accepted risks (documented, not fixed here)

1. **Well-known simulator webhook secret.** `DEFAULT_WEBHOOK_SECRET =
   "sim-webhook-secret"` ships in source; in `SIMULATION_MODE` (the demo
   default) anyone can forge "valid" webhooks against the synthetic dataset.
   No real money moves in sim mode. Real mode fails closed: an empty
   `RAZORPAY_WEBHOOK_SECRET` rejects every webhook. Mitigation for shared demo
   deployments: set `RAZORPAY_WEBHOOK_SECRET`.
2. **Demo-grade authN/Z** (single shared `X-API-Key`, self-declared approver
   identity, open GETs, demo/detection exemption outside prod) — as designed in
   `docs/security-architecture.md` §2; production path is OIDC + role-bound
   actors.
3. **In-memory rate limiting** is per-process; multi-worker deployments need a
   shared limiter. Webhook body cap (1 MiB) now bounds per-request memory.
4. **Webhook ack `detail` echoes handler error text** (`handler error: …`) to
   the caller. Reaching it requires a valid HMAC signature; exception text can
   carry payload-derived values (attacker-influenceable but only visible to the
   secret holder). Consider truncating/sanitizing in P2.
5. **Single-writer SQLite** for the local demo (documented ceiling; Postgres
   path exists and is container-verified).

## Residual recommendations (P2, reported — not changed)

- `payment_link.paid` verification trusts `reference_id` without an amount
  cross-check against the action (identity is anchored; amount drift would be a
  gateway-side anomaly — defense-in-depth candidate).
- Detection-run and evaluation-run endpoints persist their own run records but
  do not append `audit_logs` rows; non-financial, acceptable today.
- `PolicyDecisionRecord` persistence is flush-not-commit by design (caller's
  transaction owns durability); a commit-independent audit sink is a documented
  flag in `docs/policy.md` §4.

## How to re-run

```bash
cd backend && .venv/Scripts/python -m pytest tests/security -q   # 77 adversarial tests
cd backend && .venv/Scripts/python -m pytest -q                  # full suite
```

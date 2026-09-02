# Razorpay Integration Audit — PulseRecover

Audit phase 8. Verification classes: **LIVE-VERIFIED** (observed against the real Razorpay Test Mode API from the audit account, 2026-09-01/02) · **CODE+TEST** (implementation + MockTransport tests) · **DOC-CLAIMED** (per docs/razorpay-integration.md, "verified 2026-08-28") — note: razorpay.com/docs pages are JS-rendered and two reference URLs 404'd at fetch time, so live-API behavior is treated as the stronger evidence. Account context: a standard Razorpay account in **Test Mode** with Subscriptions and the direct-Payments products **not enabled**.

## Endpoint capability matrix

| # | Endpoint | Purpose | Impl (file:line) | Test coverage | Verdict |
|---|---|---|---|---|---|
| 1 | `GET /v1/payments?count=1` | connection probe | `services/merchant/client.py:73` | tests/merchant/test_probe.py | **IMPLEMENTED — LIVE-VERIFIED** (200 with account keys) |
| 2 | `GET /v1/orders` (from/to/count/skip) | order sync | `client.get_collection_page` ← `_sync_orders` | test_sync.py (pagination proven) | **IMPLEMENTED — LIVE-VERIFIED** (windowed pull returned 200; 6 orders ingested) |
| 3 | `GET /v1/payments` (windowed) | payment sync | same path | test_sync.py | **IMPLEMENTED — LIVE-VERIFIED** (6 payments ingested; idempotent re-upsert) |
| 4 | `GET /v1/subscriptions` (windowed) | subscription sync | `_sync_subscriptions` | test_sync.py + degradation test | **UNSUPPORTED on this account — LIVE-VERIFIED 401** (product not enabled; Razorpay answers 401, not 403). Handled: probe canary + per-entity degradation (`service.py`, dcef95a) |
| 5 | `GET /v1/payment_links?reference_id=` | reconcile outbound links | `_sync_payment_links` | test_sync.py | **IMPLEMENTED** (CODE+TEST; not yet exercised live with our reference_ids — 0 outbound links exist) |
| 6 | `GET /v1/payments/{id}` | resolve UNKNOWN / verify | `services/razorpay/client.py:98` | tests/razorpay + security | **IMPLEMENTED** (CODE+TEST) |
| 7 | `GET /v1/orders/{id}` | resolve UNKNOWN | `client.py:95` | tests/razorpay | **IMPLEMENTED** (CODE+TEST) |
| 8 | `POST /v1/orders` | retry recovery action | `client.py:77` (`receipt`=gateway_request_id) | tests incl. never-retried-on-timeout | **IMPLEMENTED — LIVE-VERIFIED** (2 orders created 2026-09-02, 200) |
| 9 | `POST /v1/payment_links` | primary recovery action | `client.py:101` (`reference_id` idempotency) | tests incl. duplicate-execute race | **IMPLEMENTED — LIVE-VERIFIED** (7 links created across two batches, 200; hosted payment flow confirmed by paid links landing in sync) |
| 10 | `POST /v1/subscriptions` | subscription retry | `client.py:120` — **never called by the executor** | unit tests only | **PARTIALLY_IMPLEMENTED + UNSUPPORTED here** (dead code path; account also 401s the product) |
| 11 | `POST /v1/payments` (direct card) | — not used by the app | — | — | **NOT_IMPLEMENTED (correctly)** — probed once for seeding: **401, product not enabled** (LIVE-VERIFIED) |

## Webhook intake

- Signature: HMAC-SHA256(`RAZORPAY_WEBHOOK_SECRET`, raw body) vs `X-Razorpay-Signature`, fail-closed 400 on missing/mismatch — **LIVE-VERIFIED both directions** (400 on wrong secret during the 2026-09-02 mismatch incident; 200 + stored row after alignment).
- Dedupe: `X-Razorpay-Event-Id` UNIQUE constraint, `already_processed` ack (CODE+TEST).
- Raw-body cap 1 MiB pre-verification (CODE+TEST).
- Handler registry (`webhook_handlers.py:233-237`): exactly **`payment.captured`, `payment.failed`, `payment_link.paid`**. Unknown types are stored with "no handler registered" (processed=true, inert).

### DEFECT (confirmed) — documented subscription list is inverted vs the registry

- docs/razorpay-integration.md §Webhook (and the render.yaml comment, and the audit-session dashboard instructions) tell the operator to subscribe to: `payment.captured, payment.failed, order.paid, refund.processed, subscription.charged, subscription.charge_failed`.
- Reality: the last three (+ `order.paid`) have **no handlers**, and **`payment_link.paid` — the only event that verifies a link-based recovery (`_mark_action(..., RECOVERED, "payment_link.paid")`, webhook_handlers.py:227) — is NOT in the documented list.** The live Razorpay dashboard was configured from those docs, so the deployment's subscription set is wrong.
- Mitigation that already exists: GET-based resolve (executor `resolve` + reconcile sweep) can still close VERIFYING actions without the event.
- Fix required: docs + render.yaml comment + dashboard subscription must be `payment.captured, payment.failed, payment_link.paid` (the rest optional/stored-only); docs/razorpay-integration.md's handler list (6 handlers claimed) must be corrected to the actual 3.

## Test-Mode supportability of recovery actions (this account)

| Action | Test Mode support | Evidence |
|---|---|---|
| `create_payment_link` (recovery primary) | **Full** — link creation + hosted payment + webhook | LIVE-VERIFIED end-to-end |
| `create_order` (retry) | Create yes; payment requires hosted checkout (direct payments API 401) | LIVE-VERIFIED |
| `create_subscription` | No (product disabled) | LIVE-VERIFIED 401 |
| Refund / grace-period / pause-resume subscription | Not callable — **no executor mapping** (`executor.py:923-928`, UNSUPPORTED_ACTION on fire) | CODE+TEST |
| Customer notification (`notify_customer`) | No external channel — both senders simulated (`worker/senders.py`) | CODE+TEST |

## Doc-vs-reality corrections for docs/razorpay-integration.md

1. Handler list: 6 claimed → **3 actual** (see defect above); subscription list must include `payment_link.paid`.
2. Test Mode Limitations table: add the now-proven row — disabled products answer **401** (not 403) on their list endpoints (subscriptions) and on `POST /v1/payments`; sync must degrade per entity (already shipped, dcef95a).
3. "Endpoints Used" §8 (`POST /v1/subscriptions`): mark as **never invoked by the executor** (no caller), not merely "no idempotency".
4. Everything else spot-checked (Basic auth, window caps 180d, count/skip pagination, error→typed-error mapping, GET-only retry policy, never-retry-mutations) matches code exactly.

## Verdict

The integration's **read path is real, current, and live-proven**; the **write path that matters (payment links) is real and live-proven**; the **webhook path is real and verified in both directions**. The material gaps are: the inverted webhook subscription documentation (P1 defect), unimplemented allowlisted action types, the simulated notification channel, and account-level product enablement (subscriptions/direct payments) which the app now survives gracefully.

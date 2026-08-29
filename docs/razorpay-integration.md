# Razorpay Integration — Real Adapter, Simulation Twin, Webhooks

Owner: gateway/webhook agent. Code lives in `backend/app/services/razorpay/`
plus the webhook router at `backend/app/api/v1/webhooks.py`. All Razorpay
facts below are verified in `docs/research.md` (accessed 2026-08-26); sources
at the bottom.

## 1. Real vs simulated boundary

The only coupling is the `PaymentGateway` port (`backend/app/ports.py`). Two
implementations:

| | `RazorpayGateway` (`client.py`) | `SimulatedPaymentGateway` (`simulated.py`) |
|---|---|---|
| Network | Real HTTPS via `httpx`, raw REST, no SDK | **None — in-memory, SIMULATION ONLY** |
| Auth | HTTP Basic `key_id:key_secret` | n/a |
| Outcomes | Razorpay Test Mode decides | Deterministic, seeded per entity |
| Used when | `SIMULATION_MODE=false` AND keys present | `SIMULATION_MODE=true` OR keys absent |

Selection happens in `factory.get_gateway(settings)`:

- `SIMULATION_MODE=true`, or `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` empty →
  the simulation twin. The app can never accidentally hit the network without
  credentials.
- Otherwise → the real adapter against `RAZORPAY_BASE_URL`
  (default `https://api.razorpay.com/v1`). Test vs live mode is selected by
  the **key** (`rzp_test_*` vs `rzp_live_*`), not the URL — both use the same
  base URL.

`/api/v1/system/health` reports `gateway.detail` as `simulator` or
`razorpay_test` via the same `use_simulator()` predicate the factory uses, so
the reported mode is always the mode actually in force.

### Simulator determinism

The twin is modeled on documented Razorpay API semantics + test-mode
behaviors (docs/research.md) — no proprietary Razorpay infrastructure,
routing, issuer, or network telemetry is used or implied.

`SimulatedPaymentGateway(seed=...)` derives every outcome from
`random.Random(f"{seed}:{entity-key}")`, so results depend only on the seed —
not call order or wall clock (`base_ts` fixes timestamps). Two instances with
the same seed produce byte-identical payloads (verified in
`tests/razorpay/test_simulated.py`). An optional `incident` context perturbs
outcomes for degradation scenarios:

- `{"outage": True}` — mutating calls raise `GatewayTransientError` (simulated
  5xx/timeout ambiguity),
- `{"success_rate": 0.0..1.0}` — overrides the payment-link payment rate,
- `{"error_reason": "bank_technical_error"}` — forces the failure taxonomy.

The simulator mirrors Razorpay's dedupe semantics: duplicate order `receipt`
→ 400 "same receipt value", duplicate payment-link `reference_id` → 400
"existing reference id". It also exposes `build_event(...)` (SIMULATION ONLY)
to mint correctly-signed webhook deliveries for end-to-end tests.

## 2. Test-mode setup (from research.md)

1. Razorpay Dashboard → toggle **Test Mode**, generate keys (`rzp_test_*`).
2. Configure `.env` (never commit real secrets):

   ```
   SIMULATION_MODE=false
   RAZORPAY_KEY_ID=rzp_test_...
   RAZORPAY_KEY_SECRET=...
   RAZORPAY_WEBHOOK_SECRET=...      # per-webhook secret, need not equal API secret
   RAZORPAY_BASE_URL=https://api.razorpay.com/v1
   ```

3. Dashboard → Webhooks: add a public URL (`POST /webhooks/razorpay`), choose
   events (`payment.captured`, `payment.failed`, `payment_link.paid`, …), set
   the webhook secret. Separate test/live webhook configs; TLS 1.2+ required.
4. Deterministic failure simulation for demos: dedicated test cards per
   `error_reason` (`4100280000080001` → `insufficient_fund`,
   `4100280000090000` → `payment_timed_out`, …), test UPI handles
   `success@razorpay` / `failure@razorpay` with amount-based triggers.
5. Test-mode limits that shape the demo: **max 30 payment links per
   business**, UPI payment links unsupported, card tokens valid 3 days.

## 3. Idempotency & dedup design

Razorpay's idempotency is uneven (research.md §Idempotency landscape), so
PulseRecover enforces its own execution ledger:

| Surface | Gateway-side dedupe | PulseRecover guard |
|---|---|---|
| Orders | unique `receipt` (≤40 chars) | we send `gateway_request_id` as `receipt` |
| Payment Links | unique `reference_id` (≤40) | we send `gateway_request_id` as `reference_id` |
| Subscriptions | **none** | ledger only: `recovery_actions.gateway_request_id` UNIQUE; the id is also copied into `notes` for traceability; never retried |
| Refunds | `X-Refund-Idempotency` header | **we never auto-refund** (policy hard block) |
| Webhooks | `x-razorpay-event-id` unique per event | `webhook_events.gateway_event_id` UNIQUE constraint |

Rules enforced by the adapter (`client.py`):

- Every outbound mutating call is sent **exactly once**. On timeout,
  connection error, malformed response, or 5xx it raises
  `GatewayTransientError`/`GatewayServerError` — an *ambiguous* outcome. The
  execution layer must mark the action `UNKNOWN`, never blind-retry, and
  resolve later by re-querying `fetch_payment` / `fetch_order`
  (`GET /v1/payments/:id`, `GET /v1/orders/:id`).
- Exponential backoff (`0.25s · 2^n`, `max_retries=3`, both injectable for
  tests) is applied **only to idempotent GETs**, including on 429.
- 4xx maps to definitive-failure errors (`GatewayBadRequestError`,
  `GatewayAuthenticationError`, `GatewayNotFoundError`,
  `GatewayRateLimitError`) carrying Razorpay's `code` / `source` / `step` /
  `reason` taxonomy from the error envelope
  (`{"error": {code, description, source, step, reason, ...}}`).

## 4. Webhook intake (`POST /webhooks/razorpay`)

Order of operations (ack target < 5s; Razorpay retries non-2xx with
exponential backoff for 24h):

1. Read the **raw** body; require `X-Razorpay-Signature` and
   `x-razorpay-event-id` (400 if missing).
2. Verify `HMAC-SHA256(webhook_secret, raw_body)` against the signature with
   constant-time compare; mismatch → 400, nothing stored. The secret fails
   closed when unconfigured.
3. Insert into `webhook_events`. A unique-constraint hit on
   `gateway_event_id` means at-least-once redelivery → **200
   `already_processed`, zero side effects** (verified by test).
4. Dispatch to a small handler registry (`EVENT_HANDLERS`):
   `payment.captured`, `payment.failed`, `payment_link.paid`. Unknown event
   types are stored and acked (`processed=true`, no-op). Handler failures are
   recorded on the row (`processed=false`, `error=...`) and still acked 200 —
   the stored event can be reconciled later; a 5xx here would be worse, since
   the redelivery would dedupe away in step 3.

### Out-of-order safety

Razorpay does not guarantee ordering, and `payment.failed` is **not**
terminal — a later `payment.captured` for the same payment is expected
behaviour (e.g. late UPI authorization). The handlers therefore implement:

- `payment.captured` always wins: a captured payment never regresses to
  failed; a late `payment.failed` after capture is a no-op.
- `payment.failed` updates failure telemetry (`error_code` / `error_source` /
  `error_description`; `error_reason` folds into the action's `last_error`)
  and transitions the payment — unless already captured.
- Linked recovery actions (`RecoveryOpportunity.payment_id → RecoveryAction`,
  or `payment_link.paid.reference_id == gateway_request_id` for
  `create_payment_link` actions) transition:
  - `payment.captured` / `payment_link.paid`: `EXECUTING | VERIFYING | FAILED`
    → `RECOVERED` (late success beats an earlier failure), setting
    `verified_at` / `completed_at` and writing an `audit_logs` row
    (`verify_recovered`, actor `system:webhook`).
  - `payment.failed`: `EXECUTING | VERIFYING` → `FAILED`; `RECOVERED` is
    terminal and never touched.
- Payment state transitions append `payment_events` rows (`source="webhook"`)
  so detection sees the same signal regardless of delivery duplicates.

## 5. Files

- `backend/app/services/razorpay/client.py` — real adapter (httpx, basic auth).
- `backend/app/services/razorpay/simulated.py` — SIMULATION twin, seeded.
- `backend/app/services/razorpay/factory.py` — `get_gateway`, `use_simulator`,
  `gateway_mode`.
- `backend/app/services/razorpay/errors.py` — typed errors + envelope mapping.
- `backend/app/api/v1/webhooks.py` — webhook intake + handler registry.
- `backend/tests/razorpay/` — MockTransport adapter tests, simulator
  determinism tests, webhook API tests (47 tests).

## 6. Sources

All verified 2026-08-26 in `docs/research.md`:

- Auth/base URL: razorpay.com/docs/api/authentication, /docs/api/sandbox-setup
- Orders (receipt dedupe): razorpay.com/docs/api/orders/create
- Payments (status enum, error taxonomy, capture semantics):
  razorpay.com/docs/api/payments/entity, /capture
- Payment Links (`reference_id` dedupe, 30-link test cap, status enum):
  razorpay.com/docs/api/payments/payment-links/*
- Subscriptions (no idempotency, lifecycle):
  razorpay.com/docs/api/payments/subscriptions/*
- Refunds (`X-Refund-Idempotency`):
  razorpay.com/docs/api/refunds/normal-refunds-idempotent
- Webhooks (raw-body HMAC-SHA256, `x-razorpay-event-id` dedup, at-least-once
  unordered delivery, 24h retry, failed-is-not-terminal):
  razorpay.com/docs/webhooks/validate-test, /best-practices,
  /payloads/payments
- Test mode (cards/UPI triggers, limitations):
  razorpay.com/docs/payments/payments/test-card-details, /test-upi-details


## Real sync design — verified API basis (2026-08-28)

Facts below were re-verified by direct page fetch on **2026-08-28** as the
basis for a real sync service. Labels: **VERIFIED(url)** = seen on the cited
official page that day · **VENDOR CLAIM** = official page asserts it, not
independently checkable · **NOT FOUND** = not published on official docs.
Canonical-host note: `docs.razorpay.com/<path>` now redirects to
`razorpay.com/docs/home` (observed 2026-08-28); the living API reference is
under `razorpay.com/docs/...`. Sections 1–6 above remain the integration
design of record; nothing here contradicts them (spot-checked §3/§4).

### A. List / retrieval endpoints

| Entity | Endpoint | Documented filters | Pagination | Extras / limits |
|---|---|---|---|---|
| Orders | `GET /v1/orders` | `authorized` (0/1), `receipt`, `from`, `to` (unix) | `count` def 10, max 100; `skip` def 0 | `expand[]` = `payments`, `payments.card`, `transfers`, `virtual_account` |
| Order | `GET /v1/orders/:id` | — | — | orders >180 days → 400 `Order older than 180 days, please use reports.` |
| Order payments | `GET /v1/orders/:id/payments` | — | — | all authorised/failed payments of the order |
| Payments | `GET /v1/payments` | `from`, `to` (unix) only | `count` def 10, max 100; `skip` | no `order_id`/`subscription_id` filter documented |
| Payment | `GET /v1/payments/:id` | — | — | full entity incl. error taxonomy (§C) |
| Payment Links | `GET /v1/payment_links` | `payment_id`, `reference_id` only | **no `count`/`skip` documented** | response = array of link objects matching the query |
| Payment Link | `GET /v1/payment_links/:id` | — | — | `payments[]` sub-array populated only after capture |
| Subscriptions | `GET /v1/subscriptions` | `plan_id`, `from`, `to` (unix) | `count` def 10, max 100; `skip` def 0 | — |
| Subscription | `GET /v1/subscriptions/:id` | — | — | — |

All VERIFIED 2026-08-28: orders razorpay.com/docs/api/orders/fetch-all/ ·
/fetch-with-id/ · /fetch-payments/; payments /docs/api/payments/fetch-all-payments/
· /entity/; links /docs/api/payments/payment-links/fetch-all-standard/ ·
/fetch-id-standard/; subs /docs/api/payments/subscriptions/fetch-subscriptions ·
/fetch-subscription-id.

### B. Envelope, auth, rate limits

- Collection envelope `{"entity":"collection","count":N,"items":[...]}`; entity
  envelope carries `entity` + `id` (identifiers are 14-char alphanumeric,
  case-sensitive). VERIFIED(razorpay.com/docs/api/understand,
  /docs/api/payments/subscriptions/entity).
- Auth: HTTP Basic — `Authorization: Basic base64(key_id:key_secret)` on every
  request. VERIFIED(/docs/api/authentication/).
- Base URL `https://api.razorpay.com/v1/` is **the same for sandbox and
  production** — test keys (`rzp_test_*`) select the sandbox (some APIs use
  v2). VERIFIED(/docs/api/sandbox-setup).
- Success = HTTP 200 per /docs/errors/ (the /docs/api/understand page also
  lists 201/202 as common success responses for some POST/PUT); failure = JSON
  error envelope (§H). VERIFIED(/docs/errors/, /docs/api/understand).
- Rate limiting: a request rate limiter exists; clients must watch for **429**
  and retry with exponential/stepped backoff plus randomisation. Numeric
  limits (req/s, buckets): **NOT FOUND** — not published.
  VERIFIED-qualitative(/docs/api/understand).
- "All Razorpay APIs are backwards-compatible." VENDOR CLAIM(/docs/api/understand).

### C. Entity fields for normalization

- **Order:** `id`, `entity`, `amount`, `amount_paid`, `amount_due`, `currency`,
  `receipt`, `status` (`created|attempted|paid`), `attempts`, `notes`,
  `created_at`, `offer_id`. VERIFIED(/docs/api/orders/fetch-all/).
- **Payment:** `id`, `entity`, `amount`, `currency`,
  `status` (`created|authorized|captured|refunded|failed`),
  `method` (`card|netbanking|wallet|emi|upi`), `order_id`, `international`,
  `refund_status` (`null|partial|full`), `amount_refunded`, `captured` (bool),
  `email`, `contact`, `fee`, `tax`, **`error_code`, `error_description`,
  `error_source`, `error_step`, `error_reason`**, `notes`, `created_at`,
  `card{...}`, `upi{...}`, `wallet`, `bank`, `vpa`, `acquirer_data`,
  `token_id`, `invoice_id`. VERIFIED(/docs/api/payments/entity/).
- **Payment Link:** `id`, `amount`, `amount_paid`, `accept_partial`,
  `first_min_partial_amount`, `reference_id`,
  `status` (`created|partially_paid|expired|cancelled|paid`), `customer{...}`,
  `expire_by` (default and max 6 months from creation), `expired_at`,
  `cancelled_at`, `payments[]` (post-capture only; `method` enum
  `netbanking|card|wallet|upi|emi|bank_transfer`), `short_url`, `notify`,
  `reminder_enable`, `notes`, `upi_link`, `user_id`.
  VERIFIED(/docs/api/payments/payment-links/fetch-all-standard/).
- **Subscription:** `id`, `plan_id`, `customer_id`,
  `status` (`created|authenticated|active|pending|halted|cancelled|completed|expired`),
  `current_start`, `current_end`, `ended_at`, `charge_at`, `auth_attempts`,
  `total_count`, `paid_count`, `remaining_count`, `quantity`, `start_at`,
  `end_at`, `expire_by`, `short_url`, `has_scheduled_changes`,
  `change_scheduled_at`, `customer_notify`, `notes`, `created_at`,
  `source` (`api|dashboard|links`).
  VERIFIED(/docs/api/payments/subscriptions/fetch-subscriptions, /entity).

### D. Webhook event map (PulseRecover lifecycle)

Envelope for all events:
`{"entity":"event","account_id","event","contains":[...],"payload":{<entity>:{"entity":{...}}},"created_at"}`
plus headers `X-Razorpay-Signature` and `x-razorpay-event-id`.
VERIFIED(samples on each page cited below).

| Event | `contains` | Carries amounts / method / error fields | Source (VERIFIED 2026-08-28) |
|---|---|---|---|
| `payment.authorized` | `["payment"]` | amount, currency, method, status=authorized | /docs/webhooks/payments/ |
| `payment.captured` | `["payment"]` | amount, method, fee/tax, status=captured | /docs/webhooks/payments/ |
| `payment.failed` | `["payment"]` | amount, method + **error_code/description/source/step/reason** | /docs/webhooks/payments/ |
| `order.paid` | `["payment","order"]` | payment entity + order (amount, amount_paid, amount_due, receipt, status=paid) | /docs/webhooks/orders/ |
| `payment_link.paid` | `["payment_link","order","payment"]` | link (amount, amount_paid, reference_id, status=paid) + order + payment (method) | /docs/webhooks/payment-links/ |
| `payment_link.partially_paid` | `["payment_link","order","payment"]` | same trio; status=partially_paid, amount_paid < amount | /docs/webhooks/payment-links/ |
| `payment_link.cancelled` | `["payment_link"]` | link only (status, cancelled_at) | /docs/webhooks/payment-links/ |
| `payment_link.expired` | `["payment_link"]` | link only (status, expired_at) | /docs/webhooks/payment-links/ |
| `subscription.authenticated` | `["subscription"]` | subscription entity | /docs/webhooks/subscriptions/ |
| `subscription.activated` | `["subscription"]` | subscription entity (status=active) | /docs/webhooks/subscriptions/ |
| `subscription.charged` | `["subscription","payment"]` | sub + payment (amount, method, invoice_id, status=captured) | /docs/webhooks/subscriptions/ |
| `subscription.completed` | `["subscription","payment"]` | sub (status=completed) + payment | /docs/webhooks/subscriptions/ |
| `subscription.pending` | `["subscription"]` | sub entity; re-fires on each failed charge retry while pending | /docs/webhooks/all/, /docs/webhooks/subscriptions/ |
| `subscription.halted` | `["subscription"]` | sub entity; retries exhausted; manual customer action required | /docs/webhooks/subscriptions/ |
| `subscription.cancelled` | `["subscription"]` | sub entity | /docs/webhooks/subscriptions/ |
| `subscription.paused` / `.resumed` / `.updated` | `["subscription"]` | sub entity | /docs/webhooks/subscriptions/ |

Payload semantics, VERIFIED(/docs/webhooks/payments/):

- A payload is a **snapshot of the entity when the event occurred** (a
  `payment.authorized` arriving late still shows status `authorized`).
- `payment.captured` carries only the payment entity; `order.paid` carries
  order + payment — official comparison on the payments webhook page.
- **`payment.failed` is not terminal**: `payment.failed` followed by
  `payment.captured` for the same transaction is documented "expected
  behaviour" (late authorisation; UPI TPAP in-app retries). This is the
  upstream basis for the §4 out-of-order rules.
- Watch Outs (verbatim): "The webhook sequence is not fixed in the JSON
  payload for payment events. payment.failed is not triggered if the payment
  fails during authorisation (while making the first payment)." Also: do not
  hardcode `vpa` — it can be absent on failed UPI Intent payments.

### E. Delivery, retry, dedup

VERIFIED 2026-08-28: /docs/webhooks/best-practices/, /docs/webhooks/faqs/,
/docs/webhooks/validate-test/.

- Signature: `X-Razorpay-Signature` = HMAC-SHA256 keyed by the **webhook
  secret** over the **raw request body**; the webhook secret need not equal
  the API key secret; after rotation, the old secret validates older events.
- Dedup: `x-razorpay-event-id` is unique per event → consumers must be
  idempotent; delivery is **at-least-once**; a response slower than **5s**
  counts as a timeout and triggers redelivery; **ordering is not guaranteed**.
- Retry: any non-2xx = delivery failure → **exponential backoff for 24h** from
  the event creation timestamp; after 24h of continuous failure the webhook is
  **disabled** and an alert email is sent; re-enable manually on the Dashboard.
  Exact retry intervals/attempt counts: **NOT FOUND**.
- Replay: via Razorpay support ticket only; event must be ≤**15 days** old;
  no bulk replay.
- Config: up to **30 webhook URLs** for Payments; public URLs only (localhost
  rejected with "private ip found for host"); blacklisted tunnel/test domains
  include ngrok.io, webhook.site, requestbin.com, hookbin.com, beeceptor.com,
  mockbin.org, loca.lt — **zrok** is the documented localhost-tunnel option;
  production endpoints require TLS ≥1.2; webhook egress IPs are published at
  /docs/security/whitelists but signature verification is recommended even
  when IPs are whitelisted.
- Test/live parity: test-mode transactions trigger test webhooks and the
  payload structure is identical in both modes.

### F. Idempotency / dedup per endpoint

| Surface | Gateway mechanism | VERIFIED (2026-08-28) |
|---|---|---|
| Orders create | `receipt` (≤40 chars, unique) — docs: "receipt is treated as an idempotency key, so a second create call with the same value is rejected." | /docs/api/orders/create/ |
| Payment Links create | `reference_id` (≤40 chars) — "Must be a unique number for each Payment Link." | /docs/api/payments/payment-links/create-standard/ |
| Refunds (normal + instant) | `X-Refund-Idempotency` header; key ≥10 chars, `[A-Za-z0-9_-]` only | /docs/api/refunds/normal-refunds-idempotent/ |
| Subscriptions create | none documented | NOT FOUND |
| Webhooks | `x-razorpay-event-id` per event (§E) | /docs/webhooks/validate-test/ |

Consistent with the §3 design table; no changes required there.

### G. Test-mode mechanics (for real test-mode sync)

- Test mode = sandbox replica of the account: no real money, test entities
  never appear in live mode, available at signup; keys are per-mode (Dashboard
  → Account & Settings → API Keys → Generate Key).
  VERIFIED(/docs/payments/dashboard/test-live-modes, /docs/api/authentication/).
- Same base URL for test and live; the key selects the mode.
  VERIFIED(/docs/api/sandbox-setup).
- Success cards (random CVV + any future expiry): Visa 4100 2800 0000 1007,
  Mastercard 5500 6700 0000 1002, RuPay 6527 6589 0000 1005, Diners
  3608 280009 1007, Amex 3402 560004 01007.
  VERIFIED(/docs/payments/payments/test-card-details).
- Failure cards (must select "failure" on the mock success/failure screen;
  Visa / Mastercard pairs): `insufficient_fund` 4100 2800 0008 0001 /
  5305 6200 0005 0001; `payment_timed_out` 4100 2800 0009 0000 /
  5305 6200 0006 0000; `payment_cancelled` 4100 2800 0007 0002 /
  5305 6200 0004 0002; `card_declined` 4100 2800 0006 0003 / 5305 6200 0003 0003
  (plus two more pairs); `card_disabled_for_online_payments`
  4100 2800 0003 0006 / 5305 6200 0000 0006; `card_number_invalid`
  4100 2800 0001 0008 / 5305 6200 0008 0008; `gateway_technical_error`
  4100 2800 0002 0007 / 5305 6200 0009 0007; `authentication_failed`
  4100 2800 0000 0009 / 5305 6200 0007 0009. VERIFIED(same page).
- International cards: MC 5555 5555 5555 4444, 5105 1051 0510 5100,
  5104 0600 0000 0008; Visa 4012 8888 8888 1881. Subscription auth cards:
  Visa credit 4718 6091 0820 4366 (domestic), MC credit 5104 0155 5555 5558
  and MC debit 5104 0600 0000 0008 (international). EMI: MC 5241 8100 0000 0000.
  VERIFIED(same page).
- UPI: `success@razorpay` / `failure@razorpay`. Watch Out (verbatim): "In test
  mode, payment cancellation will result in a successful payment. Use the live
  mode to test payment cancellation on UPI." Amount-based outcome triggers:
  **NOT FOUND** on the current page (differs from research.md §Test Mode —
  treat as unconfirmed). VERIFIED(/docs/payments/payments/test-upi-details).
- Netbanking and wallets: a mock bank/wallet page offers an explicit
  success/failure choice — no real bank redirect in test mode.
  VERIFIED(/docs/payments/payment-gateway/web-integration/custom/test-integration).
- Payment Links: **max 30 per business in test mode** ("Test Mode Limit";
  contact support for more). VERIFIED(/docs/payments/payment-links/create/).
- Subscriptions: the Dashboard **"Charge this now"** button simulates due
  charges in test mode (fires `subscription.charged`; also
  `subscription.activated` once `start_at` has passed); the authorisation
  payment for a future-start subscription charges a ₹5 token amount that is
  immediately refunded. VERIFIED(/docs/payments/subscriptions/test).
- Card-token 3-day test validity and UPI Payment Link test-mode support:
  **not re-verified 2026-08-28** — research.md (2026-08-26) entries stand but
  should be treated as unconfirmed for sync design.

### H. Errors & auth edge cases

- Error envelope: `{"error": {code, description, field, source, step, reason,
  metadata: {payment_id, order_id}}}`. VERIFIED(/docs/errors/).
- Status vocabulary: 400 client error, 401 = unauthenticated (bad/absent
  credentials), 404, **429 throttling**, 500/502/503/504.
  VERIFIED(/docs/api/understand). A published 401 response-body sample:
  **NOT FOUND** — the envelope shape is the documented contract.
- `GET /v1/orders/:id` for orders older than 180 days → 400 "Order older than
  180 days, please use reports." ("The live Orders API only retains recent
  orders for direct fetch.") Deep backfill requires the Dashboard
  Reports/Settlements export. VERIFIED(/docs/api/orders/fetch-with-id/).
- IP allowlisting: send API traffic to `api.razorpay.com` (load-balanced IPs)
  or, where egress is restricted, to the static-IP host
  `prod-api-static.razorpay.com`; webhook **egress** IP list and SSL
  certificate validity table are published; signature verification is still
  recommended on top of whitelisting. VERIFIED(/docs/security/whitelists).

### I. Recommended sync sequence

1. **Connect** — HTTPS to `https://api.razorpay.com/v1`, TLS ≥1.2, honour DNS
   TTL (no aggressive DNS caching). VERIFIED(/docs/api/best-practices/).
2. **Authenticate** — Basic auth with the `rzp_test_*` pair; treat 401 as
   misconfiguration: fail closed, never retry, never log the secret.
3. **Fetch each entity** — all are idempotent GETs; retry only on 429/5xx
   with exponential backoff + jitter (§B):
   - Orders: `GET /v1/orders?from=&to=&count=100&skip=` in `created_at`
     windows; add `expand[]=payments` when per-order payment detail is wanted;
     stop when a page returns fewer than `count` items. Respect the 180-day
     fetch horizon — older history only via Reports export (§H).
   - Payments: `GET /v1/payments?from=&to=&count=100&skip=` with the same
     windowing; no order/subscription filters — join locally on `order_id`.
   - Payment Links: `GET /v1/payment_links` documents only `payment_id` /
     `reference_id` filters and **no pagination parameters** — prefer targeted
     reconciliation by `reference_id` (= our `gateway_request_id`); treat
     full-list pagination as unspecified (§K).
   - Subscriptions: `GET /v1/subscriptions?from=&to=&count=100&skip=`
     (optionally `plan_id=`).
   - Point lookups to resolve ambiguous outcomes (UNKNOWN recovery actions):
     `GET /v1/payments/:id`, `GET /v1/orders/:id`,
     `GET /v1/payment_links/:id`, `GET /v1/subscriptions/:id` — the same
     resolve-later rule as §3.
4. **Normalize** — map statuses onto the §C enums; carry the payment error
   quintet (`error_code/error_description/error_source/error_step/error_reason`)
   verbatim into failure telemetry; convert unix timestamps and subunit
   amounts at the boundary.
5. **Validate** — check envelope shape (`entity`, `id`, collection
   `{count, items}`), enum membership, and cross-field consistency (e.g.
   order `amount_paid` vs its payments); quarantine rows that fail validation
   instead of silently coercing them.
6. **Persist with provenance** — upsert keyed on the Razorpay `id`; store the
   raw payload plus provenance: `source=razorpay_api`, endpoint, query window,
   `fetched_at`, entity `created_at`. Webhooks (§D/§E) remain the low-latency
   path; polling is reconciliation/backfill only.

### J. Documented end-to-end test-mode transaction (reviewer flow)

All steps VERIFIED 2026-08-28 (pages cited in §E/§G). One-time Dashboard
setup (keys, webhook registration) is documented Razorpay setup, not product
runtime:

1. Dashboard → Test Mode → generate `rzp_test_*` keys; register a webhook on a
   public URL (or a zrok tunnel — ngrok/webhook.site are blacklisted) for
   `payment.authorized`, `payment.captured`, `payment.failed`, `order.paid`
   (add `payment_link.*` for the link flow); set the webhook secret.
2. `POST /v1/orders` with Basic auth (amount, currency, unique `receipt`).
3. Drive Razorpay Standard Checkout with `key_id` + `order_id`; pay with test
   card `4100 2800 0000 1007` (success), a failure-matrix card (choose
   "failure" on the mock screen), `success@razorpay` / `failure@razorpay`
   (UPI), or the netbanking mock page.
4. Webhooks arrive (`payment.authorized` → `payment.captured` + `order.paid`,
   or `payment.failed` carrying the error quintet); verify
   `X-Razorpay-Signature` over the raw body, dedupe on `x-razorpay-event-id`.
5. Independent verification without Dashboard access: `GET /v1/orders/:id`
   (expect `status=paid`, `attempts` ≥ 1) and `GET /v1/payments/:id` (status,
   method, error fields) — the same reads the sync service performs.

### K. NOT FOUND / open items (2026-08-28)

- Numeric API rate limits — only qualitative 429 + backoff guidance published.
- Exact webhook retry intervals / attempt counts — only "exponential backoff
  for 24 hours".
- A published 401 response-body sample (envelope documented; no example).
- Any idempotency mechanism for subscription create.
- UPI amount-based test-outcome triggers — absent from the current test-UPI
  page (research.md 2026-08-26 said otherwise; unconfirmed).
- Card-token 3-day test validity; UPI Payment Link test-mode support —
  research.md 2026-08-26 entries, not re-verified today.
- `count`/`skip` or any cursor for `GET /v1/payment_links` — undocumented;
  full-scan reconciliation of links needs a Razorpay support answer or an
  empirical check against a test account.

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

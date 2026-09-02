# Razorpay Integration — API Reference

**Verified**: 2026-08-28 against current Razorpay documentation
**Scope**: Test Mode only (`rzp_test_*` keys). Live mode (`rzp_live_*`) supported but not the primary product.

---

## Authentication

- **Scheme**: HTTP Basic Auth
- **Username**: `RAZORPAY_KEY_ID` (e.g., `rzp_test_abc123`)
- **Password**: `RAZORPAY_KEY_SECRET`
- **Header**: `Authorization: Basic <base64(key_id:key_secret)>`
- **Test Mode**: Selected by `rzp_test_*` key prefix, not base URL

---

## Endpoints Used

### 1. Connection Probe
**GET** `/v1/payments?count=1`

| Aspect | Detail |
|--------|--------|
| Purpose | Verify credentials + connectivity before sync |
| Auth | Basic (key_id:key_secret) |
| Success | 200 OK (any valid response) |
| Failure | 401 → invalid keys; timeout/unreachable → transient |
| Idempotency | Safe (GET) |
| Retry | Yes, exponential backoff (3x) |
| Implementation | `RazorpayReadClient.probe()` in `app/services/merchant/client.py:73` |

---

### 2. List Orders
**GET** `/v1/orders`

| Aspect | Detail |
|--------|--------|
| Purpose | Windowed pull of merchant orders for sync |
| Auth | Basic |
| Query Params | `from` (unix), `to` (unix), `count` (≤100), `skip` |
| Pagination | `count` + `skip`; stop on short page |
| Max Window | 180 days (Razorpay limit) |
| Default Window | 30 days (`DEFAULT_WINDOW_DAYS`) |
| Response Envelope | `{"entity":"collection","count":N,"items":[...]}` |
| Idempotency | Safe (GET) |
| Retry | Yes, exponential backoff (3x) on 429/5xx |
| Implementation | `SyncService._sync_orders()` → `RazorpayReadClient.get_collection_page()` |

**Normalization**: `normalize_order()` in `app/services/merchant/normalize.py`
- Extracts: `id`, `amount_paise`, `currency`, `receipt`, `status`, `created_at`, `notes`
- Validates required fields, quarantines on failure
- Upserts on `(source_type, external_id)` — zero duplicates

---

### 3. List Payments
**GET** `/v1/payments`

| Aspect | Detail |
|--------|--------|
| Purpose | Windowed pull of merchant payments for sync |
| Auth | Basic |
| Query Params | `from` (unix), `to` (unix), `count` (≤100), `skip` |
| Pagination | `count` + `skip`; stop on short page |
| Max Window | 180 days |
| Default Window | 30 days |
| Response Envelope | `{"entity":"collection","count":N,"items":[...]}` |
| Idempotency | Safe (GET) |
| Retry | Yes, exponential backoff (3x) on 429/5xx |
| Implementation | `SyncService._sync_payments()` → `RazorpayReadClient.get_collection_page()` |

**Normalization**: `normalize_payment()` in `app/services/merchant/normalize.py`
- Extracts: `id`, `order_id`, `amount_paise`, `currency`, `status`, `method`, `captured`, `created_at`, `email`, `contact`, `fee_paise`, `tax_paise`, `error_code`, `error_description`, `acquirer_data`
- Links to local Order via `order_id` → `Order.external_id`
- Quarantines invalid entities

---

### 4. List Subscriptions
**GET** `/v1/subscriptions`

| Aspect | Detail |
|--------|--------|
| Purpose | Windowed pull of merchant subscriptions for sync |
| Auth | Basic |
| Query Params | `from` (unix), `to` (unix), `count` (≤100), `skip` |
| Pagination | `count` + `skip`; stop on short page |
| Max Window | 180 days |
| Default Window | 30 days |
| Response Envelope | `{"entity":"collection","count":N,"items":[...]}` |
| Idempotency | Safe (GET) |
| Retry | Yes, exponential backoff (3x) on 429/5xx |
| Implementation | `SyncService._sync_subscriptions()` → `RazorpayReadClient.get_collection_page()` |

**Normalization**: `normalize_subscription()` in `app/services/merchant/normalize.py`
- Extracts: `id`, `plan_id`, `customer_id`, `status`, `current_start`, `current_end`, `ended_at`, `quantity`, `remaining_count`, `charge_at`, `created_at`
- Quarantines invalid entities

---

### 5. Fetch Payment Links (Targeted)
**GET** `/v1/payment_links?reference_id={reference_id}`

| Aspect | Detail |
|--------|--------|
| Purpose | Reconcile ONLY payment links created by PulseRecover recovery actions |
| Auth | Basic |
| Query Params | `reference_id` (maps to `RecoveryAction.gateway_request_id`) |
| Pagination | **None documented** — returns array of link objects directly |
| Filtering | By `reference_id` only (our outbound links) |
| Response | JSON array `[{...}, {...}]` or envelope `{"items":[...]}` (both handled) |
| Idempotency | Safe (GET) |
| Retry | Yes, exponential backoff (3x) on 429/5xx |
| Implementation | `SyncService._sync_payment_links()` → `RazorpayReadClient.get_collection_page()` |

**Normalization**: `normalize_payment_link()` in `app/services/merchant/normalize.py`
- Extracts embedded `payments[]` array from link object
- Each payment normalized via `normalize_payment()` and ingested
- Link itself not stored locally (only referenced via `RecoveryAction.gateway_request_id`)

---

### 6. Create Payment Link (Recovery Action)
**POST** `/v1/payment_links`

| Aspect | Detail |
|--------|--------|
| Purpose | Primary automated recovery mechanism — send payment link to customer |
| Auth | Basic |
| Idempotency Key | `reference_id` (set to `RecoveryAction.gateway_request_id`, ≤40 chars) |
| Body | `amount`, `currency`, `reference_id`, `customer{name,email,contact}`, `description` |
| Response | `{id, short_url, reference_id, status, ...}` |
| Retry | **Never** — mutating POST, no gateway-side idempotency beyond `reference_id` |
| Failure Handling | `GatewayTransientError` → action marked `UNKNOWN`, resolved by re-query |
| Implementation | `RazorpayGateway.create_payment_link()` in `app/services/razorpay/client.py:101` |

**Idempotency Reality** (verified 2026-08-28):
- Payment Links dedupe via unique `reference_id` — we send our `gateway_request_id` as `reference_id`
- Internal ledger: `recovery_actions.gateway_request_id` UNIQUE constraint
- On transient failure: action status = `UNKNOWN`, next reconciliation re-queries by `reference_id`
- **Never retry mutating POST** — ledger + fetch is the guard

---

### 7. Create Order (Recovery Action)
**POST** `/v1/orders`

| Aspect | Detail |
|--------|--------|
| Purpose | Create order for retry/charge recovery flows |
| Auth | Basic |
| Idempotency Key | `receipt` (set to `RecoveryAction.gateway_request_id`, ≤40 chars) |
| Body | `amount`, `currency`, `receipt`, `notes` |
| Response | `{id, amount, currency, receipt, status, ...}` |
| Retry | **Never** — mutating POST |
| Failure Handling | `GatewayTransientError` → action marked `UNKNOWN` |
| Implementation | `RazorpayGateway.create_order()` in `app/services/razorpay/client.py:77` |

**Idempotency Reality**:
- Orders dedupe via unique `receipt` — we send our `gateway_request_id` as `receipt`
- Internal ledger: `recovery_actions.gateway_request_id` UNIQUE
- On transient failure: action status = `UNKNOWN`, resolved by `fetch_order(receipt)`

---

### 8. Create Subscription (Recovery Action)
**POST** `/v1/subscriptions`

| Aspect | Detail |
|--------|--------|
| Purpose | Retry failed subscription charge / create new subscription |
| Auth | Basic |
| Idempotency | **None** — no gateway dedupe field |
| Body | `plan_id`, `customer_id`, `total_count`, `notes{gateway_request_id}` |
| Response | `{id, plan_id, customer_id, status, ...}` |
| Retry | **Never** — mutating POST, no idempotency |
| Failure Handling | `GatewayTransientError` → action marked `UNKNOWN` |
| Implementation | `RazorpayGateway.create_subscription()` in `app/services/razorpay/client.py:120` |

**Idempotency Reality**:
- No gateway-side idempotency for subscriptions
- Only protection: internal ledger `recovery_actions.gateway_request_id` UNIQUE
- `notes.gateway_request_id` for traceability only
- Logs warning on every call (see `client.py:128-131`)

---

### 9. Fetch Single Payment
**GET** `/v1/payments/{payment_id}`

| Aspect | Detail |
|--------|--------|
| Purpose | Verify payment state after webhook / resolve UNKNOWN actions |
| Auth | Basic |
| Idempotency | Safe (GET) |
| Retry | Yes, exponential backoff (3x) on 429/5xx |
| Implementation | `RazorpayGateway.fetch_payment()` in `app/services/razorpay/client.py:98` |

---

### 10. Fetch Single Order
**GET** `/v1/orders/{order_id}`

| Aspect | Detail |
|--------|--------|
| Purpose | Verify order state / resolve UNKNOWN actions |
| Auth | Basic |
| Idempotency | Safe (GET) |
| Retry | Yes, exponential backoff (3x) on 429/5xx |
| Implementation | `RazorpayGateway.fetch_order()` in `app/services/razorpay/client.py:95` |

---

## Webhook Handling

### Endpoint
**POST** `/webhooks/razorpay` (configured in Razorpay Dashboard)

### Signature Verification
- **Algorithm**: HMAC-SHA256(`webhook_secret`, RAW request body)
- **Header**: `X-Razorpay-Signature`
- **Verification**: `RazorpayGateway.verify_webhook_signature()` in `client.py:143`
- **Fail Closed**: Missing secret or signature → 400

### Deduplication
- **Header**: `X-Razorpay-Event-Id` (globally unique per event)
- **Storage**: `WebhookEvent.gateway_event_id` UNIQUE constraint
- **Duplicate Handling**: 200 OK `{status:"already_processed", duplicate:true}`
- **Implementation**: `razorpay_webhook()` in `app/api/v1/webhooks.py:58`

### Event Processing
- Raw body parsed **after** signature verification
- Dispatched via `EVENT_HANDLERS` registry in `app/services/recovery/webhook_handlers.py`
- Handlers: `payment.captured`, `payment.failed`, `payment_link.paid` — **exactly these three** (`EVENT_HANDLERS`, `webhook_handlers.py:233`). Every other event type is stored and acked with "no handler registered" (inert).
- **Dashboard subscription**: subscribe the webhook to exactly `payment.captured`, `payment.failed`, **`payment_link.paid`** — the third is the only event that verifies a link-based recovery (DEF-01, 2026-09-02: a subscription list missing it left recoveries parked in VERIFYING).
- Out-of-order safe: payment state machine handles late/duplicate events
- Ack target: <5 seconds

### Source Tagging
- `WebhookEvent.source = "razorpay"` (real) vs `"simulator"` (research)
- Real webhooks only affect `real_test` environment data

---

## Error Handling

### Gateway Errors (mapped in `app/services/razorpay/errors.py`)

| HTTP Status | Exception | Retryable |
|-------------|-----------|-----------|
| 400 | `GatewayResponseError` | No |
| 401 | `GatewayAuthenticationError` | No |
| 404 | `GatewayResponseError` | No |
| 422 | `GatewayResponseError` | No |
| 429 | `GatewayTransientError` | Yes (GET only) |
| 5xx | `GatewayTransientError` | Yes (GET only) |
| Timeout/Transport | `GatewayTransientError` | Yes (GET only) |

### Retry Policy
- **Idempotent GETs** (probe, fetch, list): exponential backoff (0.25s, 0.5s, 1s) × 3 attempts on transient errors
- **Mutating POSTs** (create order/link/sub): **exactly one attempt**; transient → `GatewayTransientError` → caller marks `UNKNOWN`
- **Never** retry mutating calls — ledger + re-query is the resolution path

---

## Test Mode Limitations

| Limitation | Impact | Mitigation |
|------------|--------|------------|
| Order fetch by ID only (no list beyond 180 days) | Sync window capped at 180 days | `MAX_WINDOW_DAYS = 180` enforced |
| Payment Links list has no pagination | Must fetch by `reference_id` only | Sync only reconciles our own links |
| No subscription idempotency | Cannot safely retry subscription create | Never retry; log warning; ledger guards |
| Webhook delivery not guaranteed ordered | Handlers must be out-of-order safe | Payment state machine handles this |
| Test Mode rate limits apply | Sync may hit 429 on large merchants | Backoff retry; paginate; `MAX_PAGE_SIZE=100` |
| Subscriptions product not enabled on the account | `GET /v1/subscriptions` answers **401** while all other endpoints authenticate (observed in production 2026-09-01) | Probe canary (`payments?count=1`) proves the keys first; per-endpoint 4xx then degrades per entity — skip quarantined in `entity_counts.errors`, rest of the catalog syncs |
| Payment Link `reference_id` max 40 chars | `gateway_request_id` must be ≤40 chars | Generated IDs fit (prefix + uuid8 = ~28 chars) |
| No sandbox "bulk" endpoints | Mass actions must loop individually | Recovery executor processes sequentially |

---

## Implementation Locations

| Component | File |
|-----------|------|
| Read Client (sync) | `app/services/merchant/client.py` |
| Write Gateway (recovery) | `app/services/razorpay/client.py` |
| Factory (gateway selection) | `app/services/razorpay/factory.py` |
| Sync Service | `app/services/merchant/service.py` |
| Normalization | `app/services/merchant/normalize.py` |
| Webhook Intake | `app/api/v1/webhooks.py` |
| Webhook Handlers | `app/services/recovery/webhook_handlers.py` |
| Recovery Executor | `app/services/recovery/executor.py` |
| Merchant API | `app/api/v1/merchant.py` |
| Settings UI | `frontend/src/components/settings/settings-view.tsx` |

---

## Configuration

### Required Environment Variables
```bash
# Real Razorpay Test Mode (primary product)
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...
RAZORPAY_WEBHOOK_SECRET=...  # From Razorpay Dashboard webhook config
RAZORPAY_BASE_URL=https://api.razorpay.com/v1  # Same for test/live

# Optional: Force simulator for Research Lab only
SIMULATION_MODE=false  # Default: false (real gateway when keys exist)
```

### Feature Flags
- `SIMULATION_MODE=true` — Forces simulator gateway (Research Lab only)
- `LLM_PROVIDER=none` — Disables LLM, uses heuristic reasoner

---

## Security Notes

- **Key Secret**: Never logged, never sent to frontend, only used for HTTP Basic Auth
- **Key ID**: Masked in UI (`rzp_test_••••ab12`) via `mask_key_id()` in `merchant/service.py`
- **Webhook Secret**: Different from API secret; configured in Razorpay Dashboard
- **API Key**: Separate shared secret for mutating `/api/v1` routes (`X-API-Key` header)

---

## Testing

### Unit Tests (Mock Transport)
- `RazorpayReadClient` with `httpx.MockTransport` → controlled responses
- `RazorpayGateway` with `httpx.MockTransport` → success/transient/error scenarios
- `SyncService` with MockTransport → validates upsert, quarantine, idempotency
- `webhook_handlers` with synthetic payloads → state machine verification

### Integration Tests (Real API — Manual)
1. Configure test keys in `.env`
2. Run backend: `make backend`
3. POST `/api/v1/merchant/sync` → verify orders/payments ingested
4. Create test payment in Razorpay Dashboard → verify webhook received
5. Trigger recovery → verify Payment Link created → verify webhook verifies
6. Check audit trail for full provenance

---

*Generated: 2026-08-30*
*Matches implementation as of PulseRecover v0.1.0*
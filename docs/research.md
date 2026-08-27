# PulseRecover — Research & Product Strategy

**Project:** PulseRecover — AI Payment Reliability & Revenue Recovery Engine
**Submission:** Razorpay AI Buildathon, Track 03 (AI Revenue Recovery)
**Research access date:** 2026-08-26 (all URLs verified by direct page fetch unless marked **UNVERIFIED**)

---

## Executive Summary

- Razorpay's official buildathon page confirms **Track 03 — AI Revenue Recovery** and describes the bar explicitly: *"Show measured money recovered across a batch, with compliant escalation, stopping rules, and an audit trail."* PulseRecover's architecture (detect → diagnose → bounded intervene → webhook-verify → measure) maps directly onto this language.
- Razorpay **already ships pieces** of revenue recovery: fixed T+1/T+2/T+3 subscription retries (`pending` → `halted`), Failed Payment Recovery links, Intelligent Payment Retry in checkout, Payment Links reminders, Optimizer routing, and — announced at FTX'26 — a **"Subscription Recovery Agent"** in Agent Studio. PulseRecover must acknowledge these and position in the open lane: **merchant-side success-rate anomaly detection + root-cause diagnosis + bounded, verified intervention loop**, which nothing Razorpay documents currently does.
- Integration design is well-supported: Test Mode supports **Payment Links (max 30/business) and Subscriptions** explicitly; test cards/UPI can deterministically trigger ~15 distinct `error_reason` values — ideal for demoing a diagnosis engine; failed-payment entities and webhooks carry a rich `error_code`/`error_source`/`error_step`/`error_reason` taxonomy.
- Idempotency is uneven: only **Refunds** have a true idempotency header (`X-Refund-Idempotency`); Orders use unique `receipt`, Payment Links use unique `reference_id`; Payments/Subscriptions have none — the policy engine must implement its own dedup/guards.
- Webhooks are **at-least-once, unordered**, retried with exponential backoff for 24h, deduped via `x-razorpay-event-id`, and verified via `X-Razorpay-Signature` (HMAC-SHA256 over the **raw** body). A `payment.failed` event can legitimately be followed by `payment.captured` for the same transaction — failures are not terminal.

---

## Verified Razorpay API Facts

Cross-cutting:

- **Auth:** HTTP Basic with `key_id:key_secret` — "Basic auth expects an `Authorization` header … `Basic base64token` … base64 encoded string of `YOUR_KEY_ID:YOUR_KEY_SECRET`" (https://razorpay.com/docs/api/authentication/). Test keys start `rzp_test_`, live keys `rzp_live_`.
- **Base URL:** `https://api.razorpay.com` (`/v1/...`, some `/v2`); **same URL for test and live** — mode is determined by the key (https://razorpay.com/docs/api/sandbox-setup).
- **Error envelope:** `{"error": {"code", "description", "source", "step", "reason", "metadata", "field?"}}`, e.g. `code: BAD_REQUEST_ERROR`, `reason: input_validation_failed` (https://razorpay.com/docs/api/payments/capture/).
- **Idempotency landscape:** dedicated idempotent-request docs exist **only** for Refunds (`X-Refund-Idempotency` header), Route transfers, and RazorpayX payouts. Orders treat `receipt` as an idempotency key; Payment Links treat `reference_id` uniqueness similarly; Payments/Subscriptions POSTs have **no documented idempotency** (https://razorpay.com/docs/api/refunds/normal-refunds-idempotent/, https://razorpay.com/docs/api/orders/create/).

### Orders API

- **Create:** `POST /v1/orders` — request: `amount`* (integer, smallest sub-unit; ₹299 → `29900`), `currency`* (ISO), `receipt` (string, ≤40 chars, **unique** — duplicate returns 400 "An order with the same receipt value has already been created"; receipt is the idempotency key), `notes` (≤15 KV pairs, ≤256 chars each) (https://razorpay.com/docs/api/orders/create/).
- Response: `id` (`order_...`), `entity`, `amount`, `amount_paid`, `amount_due`, `currency`, `receipt`, `status`, `attempts` (count of payment attempts), `notes`, `created_at`.
- **Status enum (complete):** `created` → `attempted` (stays until one payment is captured) → `paid` (stays `paid` even if later refunded) (https://razorpay.com/docs/api/orders/create/).
- **Fetch:** `GET /v1/orders/:id` (note: "Order older than 180 days, please use reports." — 400); **Fetch all:** `GET /v1/orders` with `authorized`, `receipt`, `from`, `to`, `count` (max 100), `skip`, `expand[]=payments` (https://razorpay.com/docs/api/orders/fetch-with-id/, /fetch-all/).
- **Fetch payments of an order:** `GET /v1/orders/:id/payments` (https://razorpay.com/docs/api/orders/fetch-payments/).

### Payments API

- Read-mostly: "You can use Payments API only to retrieve payment details or change the status from `authorized` to `captured` and **not** to collect payments" (https://razorpay.com/docs/api/payments/).
- **Capture:** `POST /v1/payments/:id/capture` — body `amount`* (subunits, must equal authorized amount), `currency`*. Only valid from `authorized` state; no idempotency key — safety is via state check (https://razorpay.com/docs/api/payments/capture/).
- **Fetch:** `GET /v1/payments/:id`; **Fetch all:** `GET /v1/payments` (`from`, `to`, `count` max 100, `skip`; **no `subscription_id` filter**); **Update:** `PATCH /v1/payments/:id/` (`notes` only) (https://razorpay.com/docs/api/payments/fetch-with-id/, /fetch-all-payments/, /update/).
- **Status enum (complete):** `created`, `authorized`, `captured`, `refunded`, `failed` (https://razorpay.com/docs/api/payments/entity/).
- **`method` enum:** `card`, `netbanking`, `wallet`, `emi`, `upi`, plus `paylater` on fetch-by-id page; a separate `provider` field covers cardless-EMI/paylater/app-based methods (https://razorpay.com/docs/api/payments/entity/, /fetch-with-id/).
- **Failure telemetry (core to PulseRecover):** `error_code` (e.g. `BAD_REQUEST_ERROR`), `error_description`, `error_source` (e.g. `customer`, `gateway`, `bank`, `issuer`), `error_step` (e.g. `payment_authentication`, `payment_authorization`), `error_reason` (e.g. `incorrect_otp`, `payment_cancelled`, `insufficient_fund`, `card_declined`) (https://razorpay.com/docs/api/payments/entity/).
- Other useful fields: `card.network` ∈ {Visa, MasterCard, RuPay, Amex, Diners, Maestro, Unknown}, `card.type` ∈ {credit, debit, prepaid}, `upi.flow` ∈ {intent, collect, in_app}, `acquirer_data` (`rrn`, `auth_code`), `refund_status` (`null`/`partial`/`full`).

### Payment Links API

Docs now live under `https://razorpay.com/docs/api/payments/payment-links/...`.

- **Create:** `POST /v1/payment_links` — `amount`* (subunits, min 100 for INR), `currency` (default INR), `accept_partial` (bool, default false), `first_min_partial_amount`, `upi_link` (bool — **not supported in Test Mode**), `description` (≤2048), `reference_id` (**unique per link**, ≤40 chars — de-facto idempotency key; duplicate → 400 "An existing reference id has been passed."), `customer{name,contact,email}`, `notify{sms,email}`, `reminder_enable` (bool), `notes`, `expire_by` (Unix ts, default 6 months, ≥15 min future), `callback_url` + `callback_method: "get"` (https://razorpay.com/docs/api/payments/payment-links/create-standard/).
- Response: `id` (`plink_...`), `status`, `short_url` (`https://rzp.io/i/...`), `amount`, `amount_paid`, `payments[]` (populated after capture), `reminders.status` ∈ {pending, in_progress, failed}, `cancelled_at`, etc.
- **Fetch:** `GET /v1/payment_links/:id`; **Fetch all:** `GET /v1/payment_links/` (filter `payment_id`, `reference_id`) (https://razorpay.com/docs/api/payments/payment-links/fetch-id-standard/, /fetch-all-standard/).
- **Cancel:** `POST /v1/payment_links/:id/cancel` — errors if already paid/partially paid or expired (https://razorpay.com/docs/api/payments/payment-links/cancel-standard/).
- **Notify/resend:** `POST /v1/payment_links/:id/notify_by/:medium`, `medium` ∈ {`sms`, `email`} → `{"success": true}` (https://razorpay.com/docs/api/payments/payment-links/resend/).
- **Status enum — CURRENT docs list 5 values: `created`, `partially_paid`, `expired`, `cancelled`, `paid`.** The historically documented `issued` is **not** in the current enum (verified across entity/create/fetch/cancel pages) (https://razorpay.com/docs/api/payments/payment-links/entity/).
- **Test-mode caps:** max **30 Payment Links per business**; UPI Payment Links unsupported (from the create-page error table).

### Subscriptions API

Docs now under `https://razorpay.com/docs/api/payments/subscriptions/...`.

- **Plans:** `POST /v1/plans` — `period`* ∈ {daily, weekly, monthly, quarterly, yearly}, `interval`* (daily plans min 7), `item{name, amount, currency, description}`* → returns `plan_...` (https://razorpay.com/docs/api/payments/subscriptions/create-plan).
- **Create subscription:** `POST /v1/subscriptions` — `plan_id`*, `total_count`* (billing cycles), `quantity`, `start_at`, `expire_by` (auth-payment deadline), `customer_notify` (default true — Razorpay sends customer comms), `addons[{item{name,amount,currency}}]` (upfront, with authorization transaction), `offer_id`, `notes`. Either `total_count` or end date, not both (https://razorpay.com/docs/api/payments/subscriptions/create-subscription).
- **Entity fields:** `id` (`sub_...`), `status`, `current_start/end`, `charge_at`, **`auth_attempts`** (charge attempts this cycle), `total_count`, **`paid_count`**, **`remaining_count`**, `short_url` (authorization link), `payment_method` (card/emandate/UPI), `has_scheduled_changes` (https://razorpay.com/docs/api/payments/subscriptions/entity).
- **Lifecycle (9 states on the States page):** `created` → `authenticated` → `active`, plus `pending`, `halted`, `paused`, `cancelled`, `completed`, `expired`. **`pending` = auto-charge failed, Razorpay keeps retrying; `halted` = retries exhausted — invoices still generated but never auto-charged; missed cycles are NOT re-attempted on return to `active`** (https://razorpay.com/docs/payments/subscriptions/states).
- **Cancel/pause/resume:** `POST /v1/subscriptions/:id/cancel` (`cancel_at_cycle_end` bool; irreversible); `.../pause` (`pause_at: "now"`; feature must be enabled by support; pausing an `authenticated` sub moves it to `cancelled`); `.../resume` (https://razorpay.com/docs/api/payments/subscriptions/cancel-subscription, /pause-subscription, /resume-subscription).
- **Update:** `PATCH /v1/subscriptions/:id` — `plan_id`, `quantity`, `remaining_count`, `start_at`, `schedule_change_at` (`now`/`cycle_end`); not for UPI/emandate (https://razorpay.com/docs/api/payments/subscriptions/update-subscription).
- **Invoices:** `GET /v1/invoices?subscription_id=:id` — invoice `status` ∈ {draft, issued, partially_paid, paid, expired, cancelled, deleted}; unpaid invoices are `issued`. **No standalone "fetch pending payments" endpoint and no standalone Addons CRUD in current docs** (https://razorpay.com/docs/api/payments/subscriptions/fetch-invoices).
- **Built-in retry logic ("Payment Retries" — Razorpay's dunning):** on charge failure → `pending` + webhook; **cards: auto-retry next day, T+1/T+2/T+3 (3 retries in a T+3 cycle), then `halted`**; UPI: same T+1..T+3 cadence; emandate: retry after confirmation of prior attempt. Customer gets failure email with card-change link; if card updated while `pending`, Razorpay auto-charges the last invoice and moves back to `active`. Webhooks `subscription.pending` (re-fires per failed retry) and `subscription.halted` (https://razorpay.com/docs/payments/subscriptions/payment-retries).
- **Recurring cap:** "Cards and UPI currently support recurring payments up to ₹15,000. Charges of higher value would automatically fail for domestic cards." (https://razorpay.com/docs/payments/subscriptions/settings).

### Refunds API

- Precondition: refunds only on **captured** payments; authorized-not-captured payments auto-refund after 3 days (https://razorpay.com/docs/api/refunds/).
- **Create:** `POST /v1/payments/:id/refund` — `amount` (omit = full refund; lesser = partial), `speed` (default `normal`, 5–7 working days; `optimum` = instant attempt, falls back to normal), `notes`, `receipt` (https://razorpay.com/docs/api/refunds/create-normal/, /create-instant/).
- **Fetch:** `GET /v1/refunds/:id`, `GET /v1/refunds/`, `GET /v1/payments/:id/refunds` (https://razorpay.com/docs/api/refunds/fetch-with-id/, /fetch-all/, /fetch-multiple-refund-payment/).
- **Status enum:** `pending`, `processed` (final), `failed` (https://razorpay.com/docs/api/refunds/entity).
- **Idempotency — YES (the only core API with it):** header `X-Refund-Idempotency`, key ≥10 chars (alphanumerics + hyphens + underscores). Same body+key replays safely; different body + same key → 400; concurrent duplicate → **409 Conflict** (retry allowed). `receipt` also dedupes: "Duplicate receipt found for this refund request." (https://razorpay.com/docs/api/refunds/normal-refunds-idempotent/, /instant-refunds-idempotent/).
- Key errors: amount > captured; already fully refunded; payment not captured; insufficient account balance; instant not supported; method unsupported; blocked by dispute (https://razorpay.com/docs/api/refunds/create-normal/).

---

## Test Mode

- **Model:** "The Test mode is a replica of your account in a sandbox environment… No real money is used." Enabled by Dashboard toggle; test keys `rzp_test_*`; same API base URL — the key selects the mode (https://razorpay.com/docs/payments/dashboard/test-live-modes, https://razorpay.com/docs/api/sandbox-setup).
- **Test cards (any random CVV, future expiry; OTP 4–10 digits = success):** Indian — Visa `4100280000001007`, MC `5555510000081006`, MC prepaid `5180287200091001`, RuPay `6527658900001005`, Diners `36082800091007`, Amex `340256000401007`. International — MC `5555555555554444`, `5105105105105100`, `5104060000000008`; Visa `4012888888881881`. **Subscriptions:** domestic Visa credit `4718609108204366`, intl MC credit `5104015555555558`. EMI: MC `5241810000000000` (https://razorpay.com/docs/payments/payments/test-card-details).
- **Test UPI:** `success@razorpay` and `failure@razorpay` — both verified as documented. Caveat: in test mode, **UPI cancellation results in a successful payment**; Collect not testable on Android (https://razorpay.com/docs/payments/payments/test-upi-details).
- **Netbanking/wallets:** mock bank page with explicit **Success/Failure** choice (https://razorpay.com/docs/payments/payment-gateway/web-integration/custom/test-integration).
- **Deterministic failure simulation (gold for PulseRecover):**
  - Cards — dedicated card numbers per `error_reason`: `payment_timed_out` (`4100280000090000`), `insufficient_fund` (`4100280000080001`), `payment_cancelled` (`4100280000070002`), `card_declined` (`4100280000060003`), `card_disabled_for_online_payments` (`4100280000030006`), `card_number_invalid` (`4100280000010008`), `GATEWAY_ERROR/gateway_technical_error` (`4100280000020007`), `authentication_failed` (`4100280000000009`) — MC equivalents documented too (https://razorpay.com/docs/payments/payments/test-card-details).
  - UPI — amount-in-paise triggers with `failure@razorpay`: `204` → `incorrect_pin`, `206` → `pin_attempts_exceeded`, `208/209` → `transaction_limit_exceeded`, `212` → `debit_instrument_blocked`, `304` → `payment_declined`, `104/106` → `bank_technical_error`, `105` → `payment_timed_out`, `406` → `duplicate_request`, etc. (https://razorpay.com/docs/payments/payments/test-upi-details).
  - Subscriptions — Dashboard **"Charge this now" → "Charge as Success / Charge as Failure"**; failed charge moves `active` → `pending` (`subscription.pending` webhook); exhausted retries → `halted` (`subscription.halted`) (https://razorpay.com/docs/payments/subscriptions/test).
- **Limitations:** card tokens valid only **3 days** in test mode; UPI cancellation not testable; cannot test subscription *update* after test charges; **30 Payment Links cap**; **UPI Payment Links unsupported**; test cards error in live mode; international payments **can** be tested (dedicated intl cards). No explicit formal doc statement found on refunds-to-real-accounts in test mode — **UNVERIFIED**.
- **Payment Links in test mode: YES** (recommended by docs). **Subscriptions in test mode: YES** (dedicated test page incl. webhook verification flow) (https://razorpay.com/docs/payments/payment-links/create, https://razorpay.com/docs/payments/subscriptions/test).

---

## Webhooks

- **Event types (recovery-relevant, exact names verified):**
  - Payments: `payment.authorized`, `payment.captured`, `payment.failed`, plus `payment.downtime.started/.updated/.resolved` (https://razorpay.com/docs/webhooks/payments, /all).
  - Payment Links: `payment_link.paid`, `payment_link.partially_paid`, `payment_link.expired`, `payment_link.cancelled` (https://razorpay.com/docs/webhooks/payment-links).
  - Subscriptions (10): `subscription.authenticated`, `.activated`, `.charged`, `.pending` (re-fires per failed retry), `.halted`, `.completed`, `.updated`, `.paused`, `.resumed`, `.cancelled` (https://razorpay.com/docs/webhooks/subscriptions).
  - Refunds: `refund.created`, `.processed`, `.failed`, `.speed_changed` (https://razorpay.com/docs/webhooks/refunds).
  - Orders: `order.paid` (payload contains both order and payment entities) (https://razorpay.com/docs/webhooks/orders).
- **Payload envelope:** `{entity: "event", account_id, event, contains: [...], payload: {payment: {entity: {...}}}, created_at}`; `contains` is an array (e.g. `order.paid` → `["payment","order"]`; `subscription.charged` → `["subscription","payment"]`). Payload is a **snapshot at event time**. `payment.failed` payloads carry the full `error_code/source/step/reason` set (https://razorpay.com/docs/webhooks/payloads/payments/).
- **CRITICAL semantics:** a `payment.failed` event **can be followed by `payment.captured`** for the same transaction (late authorization / UPI in-app retry) — "expected behaviour" per docs. Do not treat `payment.failed` as terminal (https://razorpay.com/docs/webhooks/payloads/payments/).
- **Signature verification:** header `X-Razorpay-Signature` = HMAC-SHA256(webhook_secret, **raw request body**). "Do not parse or cast the webhook request body." Secret set per-webhook in Dashboard (need not equal API secret). SDKs provide helpers (`verify_webhook_signature`, `validateWebhookSignature`) (https://razorpay.com/docs/webhooks/validate-test/).
- **Delivery/retry:** success = any 2xx within **5 seconds**; failures retried "at progressive intervals… exponential backoff… for **24 hours**" (exact intervals **not published — UNVERIFIED**); after 24h of continuous failure the webhook is **disabled** (alert email sent; manual re-enable). Manual replay via support only, events ≤15 days old (https://razorpay.com/docs/webhooks/best-practices/, /faqs/).
- **Ordering/dedup:** ordering **not guaranteed**; **at-least-once** delivery explicitly documented; dedup via **`x-razorpay-event-id`** header ("unique per event" — check before processing) (https://razorpay.com/docs/webhooks/validate-test/, /best-practices/).
- Constraints: public URL, ports 80/443, TLS 1.2+, ≤30 webhook URLs, separate test/live configs, several tunnel domains blacklisted (incl. ngrok.io, webhook.site) (https://razorpay.com/docs/webhooks/).
- **Enum caveat:** `error_source`/`error_reason` enums are inconsistent across docs pages (`bank`/`issuer` in samples vs `gateway`/`razorpay` on the errors overview) — **parse defensively, never strict-enum-match** (https://razorpay.com/docs/errors/payments/payment-methods-error-parameters vs /list).

---

## Existing Razorpay Capabilities & Overlap Risk

| Capability | What it does | Gap / why PulseRecover differs | Source |
|---|---|---|---|
| **Subscriptions "Smart Payment Retries"** | Fixed T+1/T+2/T+3 auto-retry of failed recurring charges; `pending` → `halted`; failure email + card-change link | Not configurable; no decline-code-aware or best-time logic; missed cycles never re-attempted after `halted`; no arrears collection | razorpay.com/docs/payments/subscriptions/payment-retries |
| **Failed Payment Recovery** | Auto-sends payment link via WhatsApp/email/SMS after checkout failure; vendor claims "up to 20%" recovery | One-time checkout only, blog-only (no public API/docs); doesn't diagnose *why* failures cluster | razorpay.com/blog/razorpay-failed-payment-recovery/ |
| **Intelligent Payment Retry** | In-checkout next-best-action nudge after a failure | Checkout UX only; no autonomous scheduled retry | razorpay.com/blog/razorpay-intelligent-payment-retry/ |
| **Payment Links reminders / Retry Links** | ≤3 automated reminders; SMS link back to checkout | Nudges only; no charge retry, no diagnosis | razorpay.com/docs/payments/payment-links/reminders |
| **Optimizer (Infinity Router)** | Enterprise AI routing across PGs; auto-failover; ~5% SR uplift claim | Enterprise-gated; routing is Razorpay-side, opaque; no merchant-side anomaly alerting | razorpay.com/optimizer-intelligent-payments-routing/ |
| **RAY Concierge / Agentic Dashboard** | Announced 24/7 payment analyst monitoring 20+ metrics (FTX'25) | Announcement-stage; GA **UNVERIFIED** | razorpay.com/newsroom/ (FTX'25) |
| **Agent Studio (FTX'26, 2026-03-12)** | Agent marketplace incl. **Abandoned Cart Conversion Agent, Dispute Responder, Subscription Recovery Agent (with ElevenLabs)** | ⚠️ **Direct overlap flag** — must be cited. Theirs does outreach/voice recovery; nothing published on SR-degradation detection/diagnosis or bounded test-mode execution with verification | razorpay.com/newsroom/razorpay-launches-the-worlds-first-ai-native-agent-studio-for-payments-at-ftx26-powered-by-anthropics-claude/ |
| **SR Analytics Dashboard + Downtime APIs** | SR by method/provider + top failure reasons; `GET /v1/payments/downtimes` + downtime webhooks | Pull-based dashboard, no merchant-defined threshold alerts; downtime covers bank/Razorpay outages only; no public SR-analytics API found (UNVERIFIED negatively) | razorpay.com/docs/payments/optimizer/success-rate/, /docs/api/payments/downtime/ |
| **UPI Autopay "Intelligent Revenue-Protect"** | Intelligent Retry Engine (configurable cadence, beta at FTX'26), WhatsApp-led recovery, smart routing | UPI-Autopay-scoped, beta; marketing page, not a merchant-controllable engine | razorpay.com/blog/upi-autopay-with-intelligent-revenue-protect/ |
| **Razorpay MCP Server / Remote MCP 2.0** | 35+ tools (create_order, create_payment_link, create_refund, fetch_all_payments…) at `https://mcp.razorpay.com/mcp` | Execution plumbing — PulseRecover could *use* it; it does no detection/diagnosis/policy itself | razorpay.com/blog/razorpay-remote-mcp-2-0-the-next-leap-in-ai-powered-payments/ |

**Overlap verdict:** Razorpay ships *pieces* (fixed retries, nudges, enterprise routing, announced recovery agents). Nothing documented does the full **merchant-side loop**: detect SR anomaly on the merchant's own `payment.failed` stream → diagnose the failing bank/method/error cluster → quantify revenue at risk → select the *safest* intervention under a deterministic policy → execute bounded actions in test mode → **verify via signature-checked webhooks** → measure recovered ₹. That loop is the defensible lane — and the buildathon's own Track 03 wording ("measured money recovered… stopping rules… audit trail") describes exactly it.

---

## Differentiation Strategy

1. **Lead with the closed loop, not the nudge.** Existing Razorpay recovery features are open-loop nudges (send a link, hope). PulseRecover closes the loop: every intervention is verified against `payment.captured`/`payment_link.paid`/`subscription.activated` webhooks, and recovered revenue is measured per batch — the buildathon's stated bar.
2. **Diagnosis as the hero.** Aggregate `error_source`/`error_step`/`error_reason`/`method`/`bank` from `payment.failed` into a root-cause clustering story ("SR dropped 12pp at 14:00, driven by `bank_technical_error` on UPI via bank X — infrastructure, not customer intent") — distinct from *customer-intent* failures (`insufficient_fund`, `incorrect_otp`), which get different interventions. Test mode's deterministic failure cards make this demo-able live.
3. **Deterministic policy engine with stopping rules.** Hard-decline vs soft-decline routing (never retry `card_number_invalid`; retry `insufficient_fund` at payday-aware times), network-attempt caps (≤15 resubmissions/30 days guidance), per-intervention budgets, and a full audit trail. This is engineering rigor Razorpay's fixed T+1/2/3 retry doesn't expose to merchants.
4. **Acknowledge, don't ignore, Agent Studio.** Explicitly cite the announced "Subscription Recovery Agent" and position PulseRecover as complementary/orthogonal: detection + diagnosis + *verified* bounded execution, with an auditable policy — vs. outreach-oriented agents.
5. **Use Razorpay's own primitives as the action space.** Interventions = create payment link (unique `reference_id`), resend/notify, cancel stale links, refund via `X-Refund-Idempotency`, subscription-aware nudges around `pending`/`halted` (where Razorpay's own retries stop, PulseRecover starts — e.g., arrears payment links for never-reattempted missed cycles).
6. **Safety story for test mode:** dedup via `x-razorpay-event-id`, raw-body HMAC verification, `payment.failed`-is-not-terminal handling, state-check-before-capture, unique `receipt`/`reference_id` — these are the "compliant escalation, stopping rules, audit trail" the track demands.

---

## Buildathon Intelligence

- **Official page:** https://razorpay.com/buildathon/ (fetched 2026-08-26). Tagline "Build. Show. Get hired." — a Razorpay-hosted, **student-only hiring funnel for "AI Builder Interns"**, announced ~Aug 20–22, 2026; internships from **September 2026**, in-person Bangalore. Not listed on Devfolio/Devpost/unstop/HackerEarth (searched explicitly — negative finding).
- **Track 03 verbatim:** *"AI Revenue Recovery — Find revenue that's slipping away and win it back. Build an agent that detects revenue at risk, determines the right intervention, and executes a bounded recovery workflow: from payment failures and checkout abandonment to overdue receivables."*
- Track 03 example directions: payment degradation → root cause → recovery action; checkout drop-off recovery; failed-subscription recovery; B2B receivables chaser; mandate retry sequencer; Hinglish voice recovery; promise-to-pay tracker.
- **Stated bar (verbatim):** *"Show measured money recovered across a batch, with compliant escalation, stopping rules, and an audit trail."* → This is the judging rubric in one sentence; optimize the demo for it.
- Other tracks: 01 AI Growth & Agentic Commerce; 02 AI Risk Manager; 04 AI Finance Controller; 05 Open Track.
- Offer (verbatim): **₹75,000/month stipend · 6 or 12 months (candidate's choice) · in-person Bangalore from September.** Process: pick track → build → public repo + 5-min pitch video + architecture → panel if shortlisted. No resume screening/aptitude/GD.
- **UNVERIFIED:** application deadline of **September 5, 2026** circulates on third-party blogs (velonx.in, careersincloud.com, jobseekershub.co.in) but appears **nowhere on the official page** as of 2026-08-26 — confirm directly on the site/form. Evaluation axes ("problem taste, build quality, AI judgment, failure recovery") are third-party transcription, **UNVERIFIED**. Team size, year-of-study eligibility, round dates: not published.

---

## Domain Concepts

- **Soft vs hard declines:** soft = temporary, retryable (insufficient funds, issuer unavailable, SCA required); hard = permanent, retrying pointless or prohibited (lost/stolen card, invalid number, fraud) (https://docs.stripe.com/declines/codes). Stripe's explicit non-retryable list: `incorrect_number`, `lost_card`, `stolen_card`, `pickup_card`, `revocation_of_authorization`, `authentication_required`, etc. (https://docs.stripe.com/billing/revenue-recovery/smart-retries). Stripe separates `decline_code` (why) from `advice_code` (what to do) (https://docs.stripe.com/declines/card).
- **Network retry rules:** scheme "system integrity" rules (since Apr 2021): never-approve declines → never resubmit; cannot-approve-now → retry allowed but **≤ ~15 resubmissions per 30 days**; excessive retries risk scheme fines; PSPs actively block penalizable retries (https://www.checkout.com/blog/how-and-when-to-retry-sca-related-soft-declined-transactions, https://docs.adyen.com/development-resources/refusal-reasons/). Visa's exact category labels/fees **UNVERIFIED** from primary source.
- **Involuntary churn:** **20–40% of churn is involuntary** (payment-failure-driven) (https://churnkey.co/blog/involuntary-churn/, echoed by https://www.chargebee.com/payments/retries-and-dunning/). Recurly benchmarks: involuntary churn varies by price point (0.18% median at $250+ ARPC vs 1.30% at $10–25) (https://recurly.com/research/churn-rate-benchmarks/).
- **Dunning best practices:** Stripe default = **8 tries within 2 weeks**, paired with failed-payment/expiring-card emails + hosted card-update pages (https://docs.stripe.com/billing/revenue-recovery/smart-retries, /customer-emails). Chargebee: hard declines → stop retrying + request card update; soft declines → smart retries; "nudged, not spammed" (https://www.chargebee.com/payments/retries-and-dunning/). Adyen Auto Rescue: configurable 1–48-day retry window + payment-link fallback (https://docs.adyen.com/online-payments/auto-rescue).
- **Retry timing optimization:** Stripe Smart Retries uses time-dependent ML signals (best time-of-day effects, e.g. debit cards succeeding more at 12:01 AM local) (https://docs.stripe.com/billing/revenue-recovery/smart-retries). Chargebee Revive schedules insufficient-funds retries around "paydays, salary cycles, start of month" (https://www.chargebee.com/payments/retries-and-dunning/).
- **Recovery benchmarks (vendor claims, name them as such):** Stripe — **"businesses recover 55% of failed payments on average," $8.2B recovered in 2025** (https://stripe.com/billing). Recurly — **$1.6B/yr recovered** across customers (https://recurly.com/research/churn-rate-benchmarks/). Churnkey — "up to 89%" (https://churnkey.co/blog/involuntary-churn/). Razorpay — "recover up to 20% of failed payments" (https://razorpay.com/blog/razorpay-failed-payment-recovery/).
- **Gateway/acquirer degradation:** Spreedly classifies declines as hard / soft / **outage** and does real-time failover to backup gateways on soft declines/outages (https://developer.spreedly.com/docs/recover). Razorpay exposes `payment.downtime.*` webhooks + downtimes API for bank/partner outages — detection input for PulseRecover's "infrastructure vs customer intent" split (https://razorpay.com/docs/api/payments/downtime/).
- **Abandonment:** Baymard average cart abandonment **70.22%** (50 studies); **10%** of US shoppers abandon because "the credit card was declined"; 17% website errors/crashes (https://baymard.com/lists/cart-abandonment-rate). Razorpay claims 25% of carts abandoned due to failed payments, 20–25% of payments fail for avoidable reasons, ~30% revenue lost to failed transactions, ~33% of failed transactions never re-attempted (https://razorpay.com/blog/razorpay-failed-payment-recovery/, https://razorpay.com/blog/built-to-save-businesses-over-7000-cr-in-payment-failures-razorpay-launches-optimizer-indias-first-ai-powered-infinity-router/).
- **Razorpay failure taxonomy:** downtime, bad data, issuer declines etc. (https://razorpay.com/blog/online-payments-failure-reasons/).

---

## Open Questions / Unknowns

- **Buildathon deadline** (Sept 5, 2026?) — third-party only; confirm on razorpay.com/buildathon/ or the application form. **UNVERIFIED.**
- Judging criteria/weights beyond the public "measured money recovered… stopping rules… audit trail" line; third-party "evaluation axes" **UNVERIFIED**.
- Exact webhook retry intervals/max attempts — Razorpay publishes only "exponential backoff for 24 hours". **UNVERIFIED.**
- Whether a general (non-Partner) webhook CRUD API exists — only `/v2/accounts/:id/webhooks` (Partners) documented. **UNVERIFIED** negatively.
- Agent Studio "Subscription Recovery Agent" — scope, GA status, and whether it does any detection/diagnosis: announcement-only. **UNVERIFIED** beyond the newsroom post.
- RAY Concierge GA status. **UNVERIFIED.**
- `error_reason` closed enum — lists exist per method page, but no single canonical enum; treat as open set.
- Whether refunds/mandates have additional undocumented test-mode limitations — docs state limitations only in scattered form. **UNVERIFIED.**
- Public SR-analytics API for merchants — none found; **UNVERIFIED** negatively (dashboard is pull-based).
- Visa/Mastercard primary-source retry-category labels and exact penalty fees — only PSP secondary sources verified. **UNVERIFIED** at network level.

---

## Source List

All accessed 2026-08-26.

**Razorpay API docs:**
- https://razorpay.com/docs/api/authentication/
- https://razorpay.com/docs/api/sandbox-setup
- https://razorpay.com/docs/api/orders/create/ · /fetch-with-id/ · /fetch-all/ · /fetch-payments/
- https://razorpay.com/docs/api/payments/ · /entity/ · /capture/ · /fetch-with-id/ · /fetch-all-payments/ · /update/
- https://razorpay.com/docs/api/payments/payment-links/create-standard/ · /entity/ · /fetch-id-standard/ · /fetch-all-standard/ · /cancel-standard/ · /resend/
- https://razorpay.com/docs/api/payments/subscriptions/create-plan · /create-subscription · /entity · /cancel-subscription · /pause-subscription · /resume-subscription · /update-subscription · /fetch-invoices
- https://razorpay.com/docs/api/refunds/ · /entity · /create-normal/ · /create-instant/ · /normal-refunds-idempotent/ · /instant-refunds-idempotent/ · /fetch-with-id/ · /fetch-all/
- https://razorpay.com/docs/api/payments/downtime/
- https://razorpay.com/docs/errors/payments/payment-methods-error-parameters · /list · /cards

**Razorpay Test Mode:**
- https://razorpay.com/docs/payments/dashboard/test-live-modes
- https://razorpay.com/docs/payments/payments/test-card-details
- https://razorpay.com/docs/payments/payments/test-upi-details
- https://razorpay.com/docs/payments/payment-gateway/web-integration/custom/test-integration
- https://razorpay.com/docs/payments/subscriptions/test
- https://razorpay.com/docs/payments/payment-links/create

**Razorpay Webhooks:**
- https://razorpay.com/docs/webhooks/ · /all · /payments · /payment-links · /subscriptions · /refunds · /orders · /payloads/payments/ · /validate-test/ · /best-practices/ · /faqs/ · /setup-edit-payments/

**Razorpay products/blog/newsroom:**
- https://razorpay.com/docs/payments/subscriptions/states · /payment-retries · /settings · /notifications
- https://razorpay.com/blog/razorpay-failed-payment-recovery/
- https://razorpay.com/blog/razorpay-intelligent-payment-retry/
- https://razorpay.com/docs/payments/payment-links/reminders
- https://razorpay.com/optimizer-intelligent-payments-routing/
- https://razorpay.com/blog/built-to-save-businesses-over-7000-cr-in-payment-failures-razorpay-launches-optimizer-indias-first-ai-powered-infinity-router/
- https://razorpay.com/docs/payments/optimizer/success-rate/
- https://razorpay.com/blog/upi-autopay-with-intelligent-revenue-protect/
- https://razorpay.com/blog/online-payments-failure-reasons/
- https://razorpay.com/newsroom/razorpay-launches-the-worlds-first-ai-native-agent-studio-for-payments-at-ftx26-powered-by-anthropics-claude/
- https://razorpay.com/newsroom/razorpay-unveils-industry-first-innovations-at-ftx25-corporate-cards-with-yes-bank-agentic-ai-powered-payments-suite-and-indias-first-buyer-protection-program/
- https://razorpay.com/newsroom/razorpay-becomes-indias-first-payment-gateway-to-launch-mcp-server-for-instant-ai-payment-integration/
- https://razorpay.com/blog/razorpay-remote-mcp-2-0-the-next-leap-in-ai-powered-payments/
- https://razorpay.com/newsroom/razorpay-npci-and-openai-come-together-to-launch-agentic-payments-ushering-in-ai-driven-commerce-at-national-scale/
- https://www.moneycontrol.com/news/business/startup/razorpay-launches-ai-foundation-model-with-nvidia-aws-for-payment-routing-fraud-detection-14008086.html

**Buildathon:**
- https://razorpay.com/buildathon/
- https://velonx.in/blog/razorpay-ai-buildathon-2026-tracks-eligibility-stipend-selection-process (third-party)
- https://careersincloud.com/blog/razorpay-ai-buildathon-2026-75000-stipend-internship-for-students (third-party)
- https://www.jobseekershub.co.in/2026/08/razorpay-ai-buildathon-2026-bangalore.html (third-party)

**Domain concepts:**
- https://docs.stripe.com/declines/codes · /declines/card · /billing/revenue-recovery/smart-retries · /billing/revenue-recovery/customer-emails
- https://stripe.com/billing
- https://www.checkout.com/blog/how-and-when-to-retry-sca-related-soft-declined-transactions
- https://www.checkout.com/products/intelligent-acceptance
- https://docs.adyen.com/development-resources/refusal-reasons/ · /online-payments/auto-rescue
- https://www.adyen.com/press-and-media/adyen-uplift-launch · /adyen-launches-personalize · /adyen-launches-revenueaccelerate
- https://churnkey.co/blog/involuntary-churn/
- https://www.chargebee.com/payments/retries-and-dunning/ · /receivables/
- https://recurly.com/research/churn-rate-benchmarks/
- https://baremetrics.com/features/recover
- https://developer.spreedly.com/docs/recover · /routing-rules-1 · /account-updater
- https://baymard.com/lists/cart-abandonment-rate
- https://www.butterpayments.com/ (vendor claim, no methodology)
- https://www.paddle.com/retain (vendor claim)

---

# Research Refresh — 2026-08-27

Independent re-verification of official Razorpay sources (razorpay.com, docs.razorpay.com, official newsroom). Labels: VERIFIED (URL + access date) / VENDOR CLAIM / NOT FOUND. This section supersedes earlier entries where they conflict.

## Buildathon

- **VERIFIED (razorpay.com/buildathon, fetched 2026-08-27):** page unchanged. Track 03 wording and bar verbatim: *"AI Revenue Recovery — Find revenue that's slipping away and win it back. Build an agent that detects revenue at risk, determines the right intervention, and executes a bounded recovery workflow: from payment failures and checkout abandonment to overdue receivables."* Bar: *"Don't just identify the problem. Show measured money recovered across a batch, with compliant escalation, stopping rules, and an audit trail."* Offer: ₹75,000/month, 6 or 12 months, in-person Bangalore from September; public repo + 5-min pitch video + architecture.
- **NEW — VERIFIED:** official application form linked from the page (`https://forms.gle/d9r2gvxp8cmoZhon9`, "Razorpay AI Builder Internship 2026"). Graduation-year options: **2027 / 2028 / 2029 only**; in-person availability from September required. First official eligibility signal.
- **Deadline:** still NOT FOUND on official sources. "September 5, 2026" circulates on third-party blogs only — UNVERIFIED. Safe strategy: submit early.

## Razorpay product map — deltas since 2026-08-26

- **Agent Studio roster corrected:** FOUR announced agents (Abandoned Cart Conversion, Dispute Responder, **Subscription Recovery** (voice-led, ElevenLabs), **Cashflow Forecaster**) + Build Your Agent + Agentic Experience Platform (Onboarding/Dashboard/Integration). All "initial rollout"; GA unstated (VERIFIED newsroom, 2026-03-12).
- **NEW OVERLAP — RazorpayX Receivables Agent** (VERIFIED newsroom, 2026-06-01): collections follow-ups on unpaid invoices. Lands directly on Track 03's "B2B receivables" example direction → **avoid as our core**.
- **NEW OVERLAP — Sarvam partnership** (VERIFIED newsroom, 2026-03-23): Indic/Hinglish voice stack "will be integrated into Razorpay's Agent Studio" → the "Hinglish voice recovery" example direction now has a Razorpay-native path → **avoid as our core**.
- **Intelligent Retry Engine** (VERIFIED blog): beta at FTX 2026, UPI-Autopay-scoped, **merchant-configurable retry strategies** (cadence/templates/custom logic) + WhatsApp flows under "Intelligent Revenue-Protect". ⚠️ Claim adjustment: "Razorpay retries are not configurable" is now only true for the **classic Subscriptions stack** (still fixed T+1/T+2/T+3, missed cycles never re-attempted after `halted` — re-verified in raw docs HTML 2026-08-27).
- **Also mapped (context, not overlap):** Agentic Payments on Claude w/ NPCI UPI Reserve Pay (2026-02-20, pilot), Razorpay Payment CLI (2026-05-27, MCP-based) — execution surfaces we can build on.
- **Open lane reconfirmed:** nothing announced publishes detection, root-cause diagnosis, revenue-at-risk per incident, policy-gated execution, or verification semantics. See `docs/competitive-analysis.md`.

## API facts — re-verified 2026-08-27 (load-bearing, no breaking changes)

- Orders `receipt`: docs now **literally state "receipt is treated as an idempotency key"** (VERIFIED — strengthens our dedup design). NEW: 400 "another order operation is in progress" (order locked on concurrent ops).
- Refunds `X-Refund-Idempotency`: unchanged; **doc discrepancy** — prose says different-body+same-key is BAD_REQUEST (400), error table lists 409. Handle both. (We never auto-refund; noted for completeness.)
- Webhooks: at-least-once, 5s 2xx window, 24h backoff then disable + email alert, `x-razorpay-event-id` dedup, no ordering guarantee — unchanged; exact retry intervals still NOT FOUND.
- Payment Links: 5-state enum (no `issued`), `reference_id` unique ≤40 chars, `expire_by` capped at 6 months — unchanged.
- Test mode: all 8 failure-simulation cards + subscription test cards + error-reason triggers + 3-day token validity — re-verified in raw HTML.

## Adjacent-market research (2026-08-27)

Full vendor-by-vendor findings moved to **`docs/competitive-analysis.md`** (Stripe, Adyen, Chargebee, Recurly, Butter, FlexPay/Revaly, Redux, Churnkey, Baremetrics, Pagos + AIOps analogs + agentic-finance protocols). Headline: every vendor does per-transaction retry orchestration with gross-attribution measurement; none does incident detection (except Pagos, batch/files-only), diagnosis, revenue-at-risk, policy gating, or causal verification. Strategy consequences: `docs/product-strategy.md`; decisions: `docs/decision-log.md` D13–D19.

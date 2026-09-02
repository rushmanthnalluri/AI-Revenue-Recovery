# Phase B — Razorpay Capability Research (FinTech/Razorpay Specialist)

Date: 2026-09-02/03 · Author: Phase B research specialist (read-only) · Scope: current **official** Razorpay capabilities relevant to revenue recovery, judged against PulseRecover's existing integration (sync read path, payment-link create, webhook intake — `docs/audit/razorpay-audit.md`).

**Verification classes** — DOC-VERIFIED (page content fetched and read from razorpay.com/docs, mostly via the site's official `.md` markdown mirrors, e.g. `https://razorpay.com/docs/api/payments/payment-links/create-standard.md`; canonical URL cited without the suffix) · SDK-VERIFIED (official `razorpay/razorpay-python` source read) · REPO (path:line in this repository, read during this research) · AGENT-VERIFIED (fetched in full by a delegated research agent against the same official mirrors) · **UNCERTAIN** (could not verify; nothing in this document relies on an UNCERTAIN claim being true).

**Platform note:** razorpay.com/docs is now Mintlify-hosted. Several legacy GitBook URLs 404 (e.g. `/docs/api/payment-links/` → 404, confirming the Phase 8 audit note in `docs/audit/razorpay-audit.md:3`). New canonical API reference root: `/docs/api/payments/...`.

**Existing truth (not re-litigated here):** real sync works; webhooks verified both directions; worker detection every 300s; environments isolated; evaluation +0.59pp CI-crosses-zero labeled inconclusive; 993 backend tests + 9 e2e green. Account context (LIVE-VERIFIED, `docs/audit/razorpay-audit.md`): Test Mode, **Subscriptions and direct-Payments products return 401 (not enabled)**.

**Anti-creep gate used throughout:** a candidate must materially improve ≥2 of {recovery rate, safety, evidence integrity, judge comprehension, merchant value}.

---

## 1. Observations (global)

1. **PulseRecover's recovery link is created but never delivered by Razorpay today.** `create_payment_link` sends only `amount/currency/reference_id/customer/description` (REPO `backend/app/services/razorpay/client.py:101-118`). The executor *does* already assemble a `customer{name,email,contact}` payload when known (REPO `backend/app/services/recovery/executor.py:891-901`) — but because `notify` is not set, Razorpay does not send the link; delivery is the simulated outbox (`LoggingNotificationSender` / `RazorpayNotesNotificationSender` with `simulated: true`, REPO `backend/app/services/worker/senders.py:1-14`). In the real environment, **no customer ever receives the recovery link** — this is the already-tracked DEF-10 (`docs/audit/phase-a-release-gate.md:64`).
2. **The API reference documents Razorpay-handled delivery as first-class fields** — `notify.sms` / `notify.email`: *"`true`: Razorpay handles the notification"* — plus `reminder_enable`, `expire_by`, `callback_url` (DOC-VERIFIED, create-standard reference, §2). The customer's contact/email it needs is already in our payload.
3. **Recovery-window bounding exists and we don't use it.** Default link validity is **6 months** (DOC-VERIFIED). PulseRecover links today have no `expire_by`, i.e. an unbounded recovery window — at odds with our own stopping-rules story. `payment_link.expired` / `payment_link.cancelled` webhooks exist to close the loop (DOC-VERIFIED, §3).
4. **There is NO Razorpay "smart retry" for one-time payments / payment links.** Smart Payment Retries is a **Subscriptions-only** feature (T+1/T+2/T+3 daily auto-retries, `pending`→`halted`) (AGENT-VERIFIED across the full official docs index, §5). For one-time payments, merchant-side retry logic — i.e. PulseRecover — remains the product. Negative finding, strategically important.
5. **Razorpay exposes a read-only Downtime API** (`GET /v1/payments/downtimes`) with per-method/instrument severity — usable as diagnosis evidence and as a retry suppressor (DOC-VERIFIED, §6). Our simulator already models a downtime incident with `recovery_hint: "notify_customers_retry_after_downtime"` (REPO `backend/app/simulator/incidents.py:144`) and `payment_links_with_reminders` (REPO `backend/app/simulator/incidents.py:154`) — the real capabilities for both exist.
6. **No "Webhooks v2" event/payload version exists.** The only `/v2/` webhook surface is the Partners webhook-management API (`POST /v2/accounts/{id}/webhooks`), irrelevant to a standard merchant account (AGENT-VERIFIED, §4). Current intake ops facts that validate our design: endpoints must 2XX within 5s; Razorpay retries progressively for 24h then **disables** the webhook; URLs restricted to ports 80/443 (AGENT-VERIFIED, setup-edit-payments).
7. **Refunds and settlements are real, documented lifecycles** (refund: `pending/processed/failed`, events `refund.created/processed/failed/speed_changed`; settlement: `created/processed/failed`, `settlement.processed` webhook) — useful for net-recovery honesty, not for the recovery loop itself (§7, §8).
8. **Offers** can be attached to payment links (`options.order.offers`) but offer *creation* is Dashboard-only in current docs — no create-offer API (AGENT-VERIFIED, §9).

---

## 2. Payment Links: `notify.sms/email`, `reminder_enable`, `expire_by` — the real customer contact channel

**What it is (DOC-VERIFIED — `https://razorpay.com/docs/api/payments/payment-links/create-standard`):**
- `notify.sms` / `notify.email` (bool): *"`true`: Razorpay handles the notification; `false`: You handle the notification."* Razorpay sends the link to `customer.contact` / `customer.email` by SMS/email on creation.
- `reminder_enable` (bool): enables automated SMS/email reminders for the link; the reminders guide states reminders are **enabled by default** at creation, max **3 reminders**, sent only in windows **11:00–12:00 and 15:00–17:00**, and **only if customer contact/email were provided at creation** (DOC-VERIFIED — `https://razorpay.com/docs/payments/payment-links/reminders`).
- `expire_by` (Unix ts): link expiry; default/ceiling 6 months; error table implies ≥15 min in future (DOC-VERIFIED, same page).
- Response entity carries `notify{email,sms[,whatsapp]}`, `reminder_enable`, `reminders{status: pending|in_progress|failed}` (DOC-VERIFIED in create response + webhook payloads).
- `whatsapp` appears inside `notify` in webhook payload entities, but the create API documents only `sms`/`email` — WhatsApp notify settable via API: **UNCERTAIN**.
- Test Mode constraints (DOC-VERIFIED, same page): **max 30 payment links per business in Test Mode**; **UPI Payment Links not supported in Test Mode** (400).

**Candidate C1 — set `notify.sms/email=true` on recovery-link creation (Razorpay-delivered notification).**
- Evidence: DOC-VERIFIED field semantics above; REPO `client.py:101-118` (field absent today); REPO `executor.py:891-901` (customer block already passed); REPO `worker/senders.py:53-76` (current "real" sender is a no-op seam); DEF-10 in `docs/audit/phase-a-release-gate.md:64`. SDK-VERIFIED entity parity (`razorpay-python` `payment_link.py`).
- Implementation concept: extend `RazorpayGateway.create_payment_link` with `notify: dict|None` + `reminder_enable: bool|None`, pass through from the executor under a policy flag (e.g. `gateway_delivery: razorpay|none`); record `delivered_via: "razorpay_sms/email"` + the returned `notify`/`reminders` entity fields on the action receipt for provenance. `payment_link.paid` verification semantics unchanged (REPO `webhook_handlers.py:227`).
- Dependencies: customer contact/email present (already conditional in executor); a **contact-allowlist/approval policy** so we never SMS arbitrary numbers from synced data (see risks); Test-Mode 30-link budget.
- Risks: (a) **Razorpay sends a real SMS/email to whatever contact we pass — spam/compliance risk if synced test payments carry real third-party contacts** → default off, allowlist + approval-lane only; (b) whether Test Mode actually delivers SMS/email is **UNCERTAIN** (not documented) → resolve with one live probe to a controlled inbox/number; (c) customer details are not auto-populated on the hosted checkout (documented security policy) — cosmetic only.
- Test strategy: MockTransport unit tests asserting body fields + receipt capture (mirrors existing `tests/razorpay` patterns); policy tests (notify suppressed without allowlist); one live probe link with `notify.email=true` to a controlled address; existing `payment_link.paid` path re-proven by gate-1 click.
- Demo value: **highest in this document** — the judge watches a real Razorpay-branded SMS/email deliver the recovery link the agent just created; the last mile of the loop stops being "simulated: true".
- Complexity: **S** (one body field + provenance + policy flag; no new endpoint, no migration).
- Classification: **HIGH-VALUE** · Gate: recovery ✓ (customer actually receives the link — today nobody does), merchant value ✓, judge comprehension ✓ → **pass (3)**.
- **Recommendation: BUILD NOW** (strongest candidate — see §10).

**Candidate C2 — `reminder_enable=true` on recovery links (Razorpay-run follow-up cadence).**
- Evidence: DOC-VERIFIED reminders guide + create reference; REPO `simulator/incidents.py:154` already models `payment_links_with_reminders` as a recovery hint — this grounds the simulation in a real switch.
- Implementation concept: same body-field change as C1 (one more boolean); optionally mirror `reminders.status` into the action detail for evidence.
- Dependencies: C1's customer-contact requirement (reminders only fire if contact/email were provided); account-level reminder schedule is a Dashboard setting (per-link enable is API) — acceptable.
- Risks: reminder cadence is Razorpay-controlled (fixed dispatch windows, max 3) — our cooldown policy must treat Razorpay reminders as contact attempts to avoid double-nudging when combined with C3.
- Test strategy: unit (field passed, receipt stores status); live probe observing `reminders.status` transitions on an unpaid link.
- Demo value: high — "the platform itself chases the customer on a schedule; our agent's attempt budget stays intact."
- Complexity: **XS–S**.
- Classification: **HIGH-VALUE** · Gate: recovery ✓, merchant value ✓, evidence ✓ → **pass (3)**.
- **Recommendation: BUILD NOW (bundle with C1 — same code path).**

**Candidate C3 — `POST /v1/payment_links/{id}/notify_by/{sms|email}` as a policy-gated re-nudge from the worker outbox.**
- What it is (DOC-VERIFIED — `https://razorpay.com/docs/api/payments/payment-links/resend`): send/resend the link notification; response `{"success": true}`; **documented 429 per-link/per-medium rate limit**; guide states resend applies to links in `issued` state (AGENT-VERIFIED — `https://razorpay.com/docs/payments/payment-links/resend`). SDK-VERIFIED: `PaymentLink.notifyBy(id, medium)`.
- Implementation concept: map the existing notification outbox (`worker/senders.py`, `NotificationSender` port) to a `RazorpayLinkNotifySender` that calls `notify_by` for link-backed actions; the documented 429 becomes the natural cooldown contract; reuses existing outbox retry/backoff.
- Dependencies: C1 (link + contact exist); stored `plink_` id on the action (already kept for `payment_link.paid` cross-check); cooldown policy mapping.
- Risks: 429 handling must not burn worker retries (map to typed transient + backoff, per existing `_request` policy); `issued`-state-only constraint → fetch-link pre-check or tolerate typed 400; same contact-allowlist policy as C1.
- Test strategy: MockTransport tests for success/400/429 mapping; outbox integration test with fake sender; live probe resend on an own-created link.
- Demo value: medium-high (visible re-nudge with a real rate-limit contract — a stopping-rules story judge can check).
- Complexity: **M** (new gateway method + sender + policy wiring).
- Classification: **POSSIBLE → HIGH-VALUE once C1 lands** · Gate: recovery ✓, safety ✓ (bounded, rate-limited contact), merchant value ✓ → **pass (3)**.
- **Recommendation: BUILD NOW if bundling the full notification story, otherwise BUILD LATER (immediately after C1/C2).**

**Candidate C4 — `expire_by` on recovery links + handlers for `payment_link.expired` / `payment_link.cancelled`.**
- What it is (DOC-VERIFIED — states page `https://razorpay.com/docs/payments/payment-links/states/` + webhooks page §3): link lifecycle `created → partially_paid/paid/expired/cancelled`; expired/cancelled links are terminal and inaccessible.
- Evidence: REPO `client.py:101-118` (no `expire_by` today → 6-month default window); REPO `webhook_handlers.py:233-237` (registry has only 3 handlers; expired/cancelled currently stored inert).
- Implementation concept: executor sets `expire_by` from policy (e.g. recovery-window hours, ≥15 min per API constraint); add two small webhook handlers flipping the linked action to a terminal `EXPIRED`/`CANCELLED` state with audit rows (same `_mark_action` pattern as `webhook_handlers.py:227`); sync's payment-link reconcile (`_sync_payment_links`) picks up status as a second source — dedupe rules already exist.
- Dependencies: recovery-window policy value; no schema change required if terminal statuses reuse existing enums (verify; otherwise trivial enum migration).
- Risks: expiry racing a late `payment_link.paid` — handler must be idempotent and must not override a RECOVERED action (existing terminal-guard pattern covers this, rehearsal H); choosing windows too short harms recovery.
- Test strategy: unit tests for both handlers (unknown-ref hold, already-terminal no-op, race with paid); e2e-style live probe with a 15–20-minute `expire_by` link watched to `payment_link.expired` (cheap, fits the demo window).
- Demo value: high — a *bounded* recovery attempt with a visible terminal outcome is the stopping-rules requirement made physical.
- Complexity: **S**.
- Classification: **HIGH-VALUE** · Gate: safety ✓ (bounded window, terminal hygiene), evidence ✓ (clean funnel: issued→reminded→paid/expired), judge comprehension ✓ → **pass (3)**.
- **Recommendation: BUILD NOW.**

---

## 3. Webhooks: event catalog, "v2", and what to add

**What it is (AGENT-VERIFIED — `https://razorpay.com/docs/webhooks/all`, `/docs/webhooks/setup-edit-payments`):**
- Full catalog relevant here: `payment.authorized/captured/failed`; `order.paid`; `payment_link.paid/partially_paid/cancelled/expired` (DOC-VERIFIED payloads, §2 sources); `refund.created/processed/failed/speed_changed`; `subscription.authenticated/activated/charged/completed/updated/pending/halted/cancelled/paused/resumed`; `settlement.processed`; `payment.downtime.started/resolved/updated`.
- **No webhooks v2 event version exists**; `/v2/` appears only in the Partners webhook-management API — not applicable to this merchant account.
- Ops contract: 2XX within 5s, progressive retries for 24h, then the webhook is **disabled**; ports 80/443 only.

**Candidate C5 — handler for `payment_link.partially_paid`.**
- Evidence: DOC-VERIFIED event + payload (Standard links only); we do not set `accept_partial` today (client body has no such field, REPO `client.py:101-118`).
- Implementation concept: only meaningful after a partial-payments policy exists (accept_partial + first_min_partial_amount); handler would record partial recovery and keep the action VERIFYING until full `payment_link.paid` — the payload cross-check machinery (`webhook_handlers.py:411+`) already speaks amount/amount_paid.
- Dependencies: partial-payment policy decision (financial-safety implications: partially recovered revenue attribution); none technical.
- Risks: attribution ambiguity (counting partial money as recovered would corrupt the honesty story — must be a distinct metric).
- Test strategy: MockTransport payload tests incl. partial-then-full sequence.
- Demo value: low-medium today (no partial links in play).
- Complexity: **M** (mostly policy, small handler).
- Classification: **POSSIBLE** · Gate: evidence ✓, recovery ~ (untested lever) → **borderline (1.5)**.
- **Recommendation: BUILD LATER (behind a partial-payments policy decision).**

**Candidate C6 — add `order.paid` handler.**
- Evidence: AGENT-VERIFIED catalog; REPO `webhook_handlers.py:233-237` (absent). Order-based retry actions (`create_order`, REPO `client.py:77-93`) are already closed via `payment.captured`, which fires for the same money and is live-proven.
- Gate: no new information over `payment.captured`; adds handler surface and a second code path for the same outcome → **fails gate (0–1)**.
- Classification: **LOW-VALUE** · **Recommendation: REJECT** (document as considered-and-rejected).

**Candidate C7 — "adopt webhooks v2".**
- Evidence: AGENT-VERIFIED non-existence (exhaustive docs-index coverage); Partners `/v2/accounts/{id}/webhooks` is a partner-platform management API, not an event version.
- Classification: **SPECULATIVE (premise false)** · **Recommendation: REJECT.** (Keep the ops facts — 5s ack, 24h disable — as validation of the existing fast-ack intake; no build item.)

---

## 4. Subscriptions: smart retry cycles (`pending`/`halted`) — and the one-time-payment negative finding

**What it is (AGENT-VERIFIED):**
- "Smart Payment Retries" is advertised on the Subscriptions product page (`https://razorpay.com/docs/payments/subscriptions/`): *"Automatic retry logic maximises successful collections."*
- Retry mechanics (`https://razorpay.com/docs/payments/subscriptions/payment-retries`): failed auto-charge → subscription `pending`; automatic reattempts **T+1, T+2, T+3** (three daily retries); still failing → **`halted`** (invoices keep generating, no auto-charge; recovery via successful charge on an old invoice or card change; once back to `active`, *previous charges are not re-attempted*). Customer gets an email link to change card/method.
- States page (`https://razorpay.com/docs/payments/subscriptions/states`): `created/authenticated/active/pending/halted/cancelled/paused/expired/completed`. **Docs discrepancy:** the API entity enum omits `paused` (`https://razorpay.com/docs/api/payments/subscriptions/create-subscription`) — flag if ever building a status enum.
- Entity fields: `status`, `charge_at`, `remaining_count`, `paid_count`, `auth_attempts` (only retry counter; no `retry` field), `short_url`.
- Webhooks `subscription.pending` / `subscription.halted` fire on each transition.
- **No equivalent exists for one-time payments/payment links** — confirmed across the full official docs index (absence-of-evidence via the official catalog). Optimizer is routing-level failover (temporary 20-min downtimes on low SR), not per-payment retries.

**Candidate C8 — subscription-aware recovery (consume `subscription.halted`/`pending`; post-halt payment-link offer).**
- Evidence: AGENT-VERIFIED docs above; **REPO truth: the audit account gets 401 on all subscription endpoints — product not enabled** (`docs/audit/razorpay-audit.md:12,41`); executor has no subscription mapping (`executor.py:923-928`); `create_subscription` is dead code (`client.py:120-141`, never called).
- Implementation concept (if product were enabled): webhook/sync on `halted` → opportunity; recovery action = payment link for the missed invoice (Razorpay deliberately does not re-attempt past charges after resume — that gap is exactly PulseRecover-shaped).
- Dependencies: **Razorpay account product enablement (out of repo control)**; plan/customer modeling we don't have.
- Risks: scope creep into billing-platform territory; evaluation would need a subscription dataset.
- Test strategy: n/a until enabled.
- Demo value: none on this account (cannot demo a 401).
- Complexity: **L, blocked.**
- Classification: **SPECULATIVE (blocked by account enablement)** · Gate: recovery ✓ + merchant value ✓ in principle, but unverifiable/unshippable now.
- **Recommendation: RESEARCH ONLY** (revisit if the merchant account enables Subscriptions).

**Candidate C9 — rely on Razorpay smart-retry for our one-time recovery payments.**
- Classification: **SPECULATIVE (premise false — capability does not exist)** · **Recommendation: REJECT.** This negative finding is itself valuable: merchant-side retry/orchestration for one-time payments is not commoditized by Razorpay; PulseRecover's engine remains the differentiator.

---

## 5. Downtime API — real gateway-health evidence for diagnosis and retry suppression

**What it is (DOC-VERIFIED — `https://razorpay.com/docs/api/payments/downtime`, `/fetch-all`):**
- `GET /v1/payments/downtimes` (+ `GET /v1/payments/downtimes/{id}`): collection of `payment.downtime` entities: `method` (card/netbanking/upi/fpx), `begin/end`, `status` (`scheduled/started/updated`), `scheduled` bool, `severity` (`high/medium/low`), `instrument` scoped to `{bank|network|issuer|psp|vpa_handle|card_type|flow}`. **No query/filter params** (400 on extras). Docs explicitly invite trying with **Test API keys** (Postman workspace note).
- Webhook alternative exists: `payment.downtime.started/resolved/updated` (AGENT-VERIFIED).
- Whether a Test Mode account sees live downtime data: **UNCERTAIN** (the endpoint exists for test keys; data feed for test accounts is not documented).

**Candidate C10 — poll Downtime API during diagnosis; annotate opportunities and suppress retries during confirmed issuer downtime.**
- Evidence: DOC-VERIFIED above; REPO `simulator/incidents.py:144` (`recovery_hint: "notify_customers_retry_after_downtime"`) — the product narrative already assumes this signal; sync client pattern for windowed GETs exists (`docs/audit/razorpay-audit.md:9-11`); GET-only retry policy already correct for it (REPO `client.py:13-16`).
- Implementation concept: add `fetch_payment_downtimes()` to the read path (merchant client or gateway client); diagnosis enriches `payment.failed` bursts with "issuer/method downtime active (severity high, source: Razorpay)"; policy gate suppresses `retry_payment` while a matching instrument is down and prefers notify/link actions — mirroring the simulated incident's hint in the real environment.
- Dependencies: none technical (read-only, same auth); UNCERTAIN test-mode data — degrade gracefully ("no ongoing downtime reported" is itself an evidence artifact).
- Risks: over-suppression if the feed is stale/empty in test mode → suppression must be time-boxed and logged, never silent; endpoint must not enter the worker hot path un-cached (poll on detection cadence, 300s).
- Test strategy: MockTransport collection parsing + no-params contract; diagnosis-enrichment unit tests (downtime-active vs clear); live probe call to confirm test-mode response shape.
- Demo value: high for judge comprehension — "before retrying, the agent checked the gateway's own health feed" is a one-line story with a visible artifact.
- Complexity: **S–M**.
- Classification: **HIGH-VALUE** · Gate: evidence ✓, safety ✓ (don't burn attempt budget into an outage), judge comprehension ✓ → **pass (3)**.
- **Recommendation: BUILD NOW.**

**Candidate C11 — `payment.downtime.*` webhook handlers.**
- Evidence: AGENT-VERIFIED event family; push instead of poll.
- Gate: same value as C10 minus polling control; test-mode delivery of these events UNCERTAIN → **pass but weak (2)**.
- Classification: **POSSIBLE** · **Recommendation: BUILD LATER** (add the three trivial handlers when C10 proves the signal; subscription-set change is config-only).

---

## 6. Refunds lifecycle — net-recovery honesty, not a recovery lever

**What it is (AGENT-VERIFIED + DOC-VERIFIED about-page `https://razorpay.com/docs/payments/refunds/`):**
- States `pending/processed/failed`; `speed_requested: normal|optimum`, `speed_processed: instant|normal` (no literal "instant" request value); normal ≈5–7 working days (docs internally inconsistent: 7–10 on another page — noted, not material here); source-refunds only.
- Endpoints: `POST /v1/payments/{id}/refund` (full or partial by amount; repeated calls = multiple partial refunds), `GET /v1/refunds/{id}`, `GET /v1/payments/{id}/refunds`, `GET /v1/refunds`, `PATCH /v1/refunds/{id}` (notes only). Refunds only on captured payments; authorized-uncaptured auto-refunds in ~3 days.
- Webhooks `refund.created/processed/failed/speed_changed` (payloads contain refund+payment).

**Candidate C12 — consume `refund.processed`/`refund.failed` to keep "money recovered" net-of-refunds.**
- Evidence: AGENT-VERIFIED above; REPO: no refund handling anywhere (executor maps no refund action, `executor.py:923-928`; registry lacks refund handlers, `webhook_handlers.py:233-237`); the honest-metrics story (GROSS/VERIFIED/INCREMENTAL, `docs/audit/phase-a-release-gate.md:16`) currently has no refund decrement.
- Implementation concept: webhook handlers + refund sync that mark a RECOVERED action "recovered-then-refunded (full/partial)" with amount; Evaluation Lab displays NET alongside GROSS. **No refund *initiation*** — out of scope and unsafe for an autonomous agent.
- Dependencies: refunds actually occurring on the account (none today); payment→action linkage exists via payment ids.
- Risks: attribution windows (late refunds); none safety-critical (read/record only).
- Test strategy: MockTransport payload → status flip; metrics unit tests (gross vs net); no live probe needed to justify build.
- Demo value: medium — a "net recovered" chip is a credibility booster with judges; zero demo-path risk.
- Complexity: **M**.
- Classification: **POSSIBLE** · Gate: evidence ✓, merchant value ✓ → **pass (2)**.
- **Recommendation: BUILD LATER** (after the BUILD NOW batch; ahead of any refund *creation* capability, which is REJECTED — see §9).

---

## 7. Settlements — reconciliation, outside the recovery loop

**What it is (AGENT-VERIFIED — `https://razorpay.com/docs/payments/settlements/`, `/docs/api/settlements/...`):** entity `setl_*` with `amount/fees/tax/utr`, states `created/processed/failed`; `GET /v1/settlements`, `/v1/settlements/{id}`, `/v1/settlements/recon/combined?year&month[&day]`; webhook `settlement.processed` (fires at transfer *initiation*; credit can lag up to 3h); on-demand settlements `POST /v1/settlements/ondemand` (feature-gated by support request; limits ₹100–₹5Cr API). **Test Mode settlement availability: UNCERTAIN** (settlements move real money; docs imply test mode has none, no explicit statement).

**Candidate C13 — settlement recon sync / `settlement.processed` handler for a "money in bank" evidence tier.**
- Evidence: AGENT-VERIFIED above; PulseRecover's evidence ladder currently tops out at captured payments.
- Gate: evidence ✓ only (settlement ≠ recovery causation; T+2 domestic cycle means it trails the demo window entirely) → **fails gate (1)**.
- Classification: **LOW-VALUE (for this product phase)** · **Recommendation: RESEARCH ONLY** (a finance-ops feature; not Phase B).

---

## 8. Offers — discount-incentivized recovery links

**What it is (AGENT-VERIFIED — `https://razorpay.com/docs/payments/offers/`, `/docs/api/payments/payment-links/offers`):** discounts/cashback auto-applied at checkout; types Instant/Cashback/Already Discounted; **creation is Dashboard-only in current docs — no create-offer API** (legacy `POST /v1/offers` gone); attach to a payment link at creation via `options.order.offers: ["offer_..."]`; **documented warning: do not enable partial payments on offer-bearing links**; availability India/Singapore.

**Candidate C14 — attach a pre-created offer to recovery links as a conversion incentive.**
- Evidence: AGENT-VERIFIED above.
- Gate: recovery ? (plausible but unmeasured incentive), merchant value ✗ (spends margin per recovery), evidence ✗ (would contaminate the measured lift — the evaluation's whole point is action attribution) → **fails gate (≤1)**.
- Risks: margin cost; evaluation contamination (lift would mix incentive effect with orchestration effect); non-automatable offer lifecycle (dashboard-only creation) breaks the agent-autonomy story.
- Classification: **SPECULATIVE** · **Recommendation: RESEARCH ONLY** (revisit only with a merchant-defined offer policy and a way to model incentive cost in evaluation).

---

## 9. Explicitly REJECTED summary

| Candidate | Reason |
|---|---|
| C9 — Razorpay smart-retry for one-time payments | Capability does not exist (Subscriptions-only); premise false |
| C7 — webhooks v2 adoption | No v2 event version exists; Partners `/v2` management API not applicable |
| C6 — `order.paid` handler | Redundant with live-proven `payment.captured`; zero new information |
| Refund *initiation* as a recovery action | Autonomous money-out is UNSAFE; no merchant-value case in recovery; violates the bounded-mutation posture (refund endpoints documented but deliberately unmapped in the executor, REPO `executor.py:923-928`) |
| UPI Payment Links | Not supported in Test Mode (DOC-VERIFIED 400) — unusable on this deployment |

---

## 10. Consolidated classification & recommendation table

| # | Candidate | What | Classification | Gate hits (need ≥2) | Complexity | Recommendation |
|---|---|---|---|---|---|---|
| C1 | `notify.sms/email=true` on recovery links | Razorpay delivers the link by SMS/email | **HIGH-VALUE** | recovery, merchant value, judge | S | **BUILD NOW** |
| C2 | `reminder_enable=true` | Razorpay auto-reminder cadence (≤3, fixed windows) | **HIGH-VALUE** | recovery, merchant value, evidence | XS–S | **BUILD NOW** (bundle C1) |
| C3 | `notify_by/{medium}` re-nudge via outbox | Worker-driven resend, 429-bounded | POSSIBLE→HIGH-VALUE | recovery, safety, merchant value | M | BUILD NOW w/ C1, else **BUILD LATER** |
| C4 | `expire_by` + `payment_link.expired/cancelled` handlers | Bounded recovery window, terminal hygiene | **HIGH-VALUE** | safety, evidence, judge | S | **BUILD NOW** |
| C5 | `payment_link.partially_paid` handler | Partial-payment recovery tracking | POSSIBLE | evidence (+recovery untested) | M | **BUILD LATER** (needs partial policy) |
| C6 | `order.paid` handler | — | LOW-VALUE | 0 | — | **REJECT** |
| C7 | "Webhooks v2" | — | SPECULATIVE (false premise) | — | — | **REJECT** |
| C8 | Subscription halted/pending recovery | Post-halt link offer | SPECULATIVE (blocked: account 401) | recovery, merchant value (in principle) | L | **RESEARCH ONLY** |
| C9 | Rely on Razorpay one-time smart retry | — | SPECULATIVE (false premise) | — | — | **REJECT** |
| C10 | Downtime API in diagnosis + retry suppression | `GET /v1/payments/downtimes` | **HIGH-VALUE** | evidence, safety, judge | S–M | **BUILD NOW** |
| C11 | `payment.downtime.*` webhooks | Push variant of C10 | POSSIBLE | evidence, safety | S | **BUILD LATER** |
| C12 | `refund.*` → net-recovered metrics | Refund lifecycle consumption | POSSIBLE | evidence, merchant value | M | **BUILD LATER** |
| C13 | Settlements/recon evidence tier | `settlement.processed`, recon API | LOW-VALUE (this phase) | evidence only | M | **RESEARCH ONLY** |
| C14 | Offers on recovery links | Discount incentive | SPECULATIVE | ≤1 | M | **RESEARCH ONLY** |

## 11. UNCERTAIN register (verify before building)

1. **Test-Mode delivery of `notify` SMS/email** — not documented; one live probe (own inbox/number) resolves it. Blocks C1's demo claim, not its code.
2. **Test-Mode downtime feed** — endpoint documented for test keys; whether test accounts see live downtime data is undocumented (C10 must degrade gracefully).
3. **Test-Mode settlements** — strongly implied absent; no explicit doc statement.
4. **WhatsApp in `notify`** — present in webhook entity payloads, absent from create-API params.
5. **Reminder defaults** — guide says reminders default-on at creation; make `reminder_enable` explicit in our payload regardless (determinism over defaults).
6. **`paused` subscription state** — in the guide, missing from the API status enum (docs discrepancy).

## 12. Doc-drift notes for `docs/razorpay-integration.md` (handoff to docs owner)

- API reference moved: `/docs/api/payment-links/*` → **`/docs/api/payments/payment-links/*`** (old URLs 404 — matches the Phase 8 audit observation).
- New capabilities to document when C1–C4 land: `notify`, `reminder_enable`, `expire_by` request fields; `notify_by` endpoint; `payment_link.expired/cancelled` events; Test-Mode 30-link cap; UPI-link test-mode restriction.
- Webhook subscription-set guidance stays as fixed in Phase A (`payment.captured, payment.failed, payment_link.paid`); add `payment_link.expired` (+ optionally `payment_link.cancelled`) when C4 lands.

## 13. Strongest BUILD NOW (specialist's pick)

**C1+C2 — let Razorpay deliver and chase the recovery link (`notify.sms/email` + `reminder_enable`).** It closes DEF-10 with a ~2-field change on an endpoint already live-proven end-to-end, converts the only simulated step in the real merchant loop (customer contact, `worker/senders.py:53-76`) into a Razorpay-operated channel with official documented semantics, requires no new endpoint/migration, and is the single highest judge-comprehension move available: a real SMS/email arriving mid-demo is the product working, not a mock. It clears the anti-creep gate on three axes (recovery, merchant value, judge comprehension). Pair it in the same PR with **C4** (`expire_by` + expired/cancelled handlers) so the newly-real contact channel is also a *bounded* one, and follow immediately with **C10** (downtime-aware diagnosis) as the evidence/safety complement.

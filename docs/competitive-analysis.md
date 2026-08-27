# PulseRecover — Competitive Analysis

**Date:** 2026-08-27 · **Sources:** `docs/research.md` (Razorpay-native, VERIFIED on official pages) and the adjacent-landscape research pass (vendor pages, VERIFIED/VENDOR CLAIM labels preserved).

## 1. Razorpay-native capabilities (the sponsor's own roadmap)

| Capability | What it does | Status (2026-08-27) | Overlap risk |
|---|---|---|---|
| Agent Studio — Subscription Recovery Agent | Voice-led outreach for failed subscription payments (w/ ElevenLabs) | Announced, "initial rollout" | Medium — outreach-only; no detection/diagnosis/verification |
| Agent Studio — Abandoned Cart Conversion Agent | Voice-led cart recovery (w/ Nugget/Zomato, SuperU) | Announced | Low-Medium — checkout abandonment adjacency |
| Agent Studio — Dispute Responder, Cashflow Forecaster | Disputes; forecasting | Announced | Low |
| Agent Studio — Build Your Agent + connectors | Custom agents (Shopify, WhatsApp, Slack, Tally…) | Announced | None — plumbing |
| RazorpayX Receivables Agent | Collections on unpaid invoices, payment reminders | Announced (Connected Banking) | **Closes the "B2B receivables" example direction — we avoid it** |
| Sarvam partnership | Indic/Hinglish voice stack → Agent Studio | Announced ("will be integrated") | **Closes "Hinglish voice recovery" — we avoid it** |
| Subscriptions built-in retries | Fixed T+1/T+2/T+3, then `halted`; missed cycles never re-attempted | GA (VERIFIED docs) | We layer on top (arrears payment links unopposed) |
| Intelligent Retry Engine (UPI Autopay) | Merchant-configurable retry cadence/templates/logic + WhatsApp flows | **Beta** (FTX 2026) | Medium — but retry-only, no detection/diagnosis; do NOT claim "Razorpay retries aren't configurable" without the "classic stack" qualifier |
| Failed Payment Recovery | Auto payment-link via WhatsApp/email/SMS after checkout failure; "up to 20%" (VENDOR CLAIM) | Blog-only, no public API | Low — nudge-only, no diagnosis or verification |
| Intelligent Payment Retry | In-checkout next-best-action nudge | GA-ish (blog) | Low |
| Optimizer / Smart Router | Random-forest routing across PGs; "5% SR uplift" (VENDOR CLAIM) | Enterprise | None — prevention-side, complementary |
| Remote MCP 2.0 / Payment CLI | 35+ tools; agent execution plumbing | GA (self-serve) | None — a surface we can ride ("built on Razorpay rails") |

**Positioning sentence (use verbatim):** "Razorpay's announced agents recover customers; PulseRecover detects and diagnoses the *degradation incident itself*, prices it, acts under a deterministic policy gate, and proves recovered revenue. Nothing announced — by Razorpay or the market — publishes detection, diagnosis, or verification semantics."

## 2. Adjacent vendors — where they stop

| Vendor | Retry orchestration | Detection | Diagnosis | Revenue-at-risk | Policy gate | Causal verification |
|---|---|---|---|---|---|---|
| Stripe (Smart Retries) | ✅ (not India-issued cards) | ❌ | ❌ (decline codes only) | ❌ | ❌ | ⚠️ gross ("by any means", VERIFIED definition) |
| Adyen (Auto Rescue/Uplift) | ✅ | ❌ | ❌ | ❌ | ❌ | ⚠️ baseline (conversion only) |
| Chargebee | ✅ (40+ gateways) | ⚠️ error monitoring | ❌ | ❌ | ❌ | ⚠️ logs |
| Recurly | ✅ | ❌ | ❌ | ❌ | ❌ | ⚠️ per-campaign |
| Butter | ✅ per-payment ML | ❌ | ❌ | ❌ | ❌ | ⚠️ per-invoice; revenue-share pricing |
| FlexPay → Revaly | ✅ | ❌ | ❌ | ❌ | ❌ | ⚠️ pre/post lift pricing (historical) |
| Redux | ✅ (Stripe-only B2C) | ❌ | ❌ | ❌ | ❌ | ⚠️ pre/post lift pricing; honesty outlier |
| Churnkey | ✅ | ❌ | ❌ | ❌ | ❌ | ⚠️ variant A/B (no holdout) |
| Baremetrics Recover | ❌ (defers to Stripe) | ❌ | ❌ | ❌ | ❌ | ⚠️ touch-attribution (weakest) |
| **Pagos** | ❌ (recommendations) | **✅ (VERIFIED)** | ⚠️ decline narratives + network benchmark | ❌ | ❌ | ❌ (daily/weekly files) |
| **PulseRecover** | ✅ (bounded, gated) | **✅** | **✅ (ML + AI investigation)** | **✅ (counterfactual, CIs)** | **✅ (deterministic, fail-closed)** | **✅ (webhook-verified + holdout lift — P1)** |

## 3. Observability/AIOps analogs (what we borrowed)

- **Datadog Watchdog:** zero-config anomaly feed, expected-bounds overlay, query-time outlier attribution (→ our decline-outlier facets), RCA discipline: root cause = state change, symptom ≠ cause.
- **PagerDuty Automation Actions:** diagnostic-vs-remediation action library, invocation guards, structured on-incident action log.
- **incident.io:** diagnosis pre-computed at detection time, not on human demand.
- **Dynatrace Davis:** "approved actions under policy guardrails" (Preview) — mainstream validation of our policy-gate design.
- **Pagos:** merchant-vs-network benchmark callouts; "flat fee, no percentage of recovered revenue" pricing posture.

## 4. Agentic-finance guardrail landscape (validation + patterns)

- **AP2 (Google, spec-draft, FIDO Alliance):** cryptographically signed mandate chains; **Trusted Surface MUST be non-agentic deterministic code** — our ADR 0003/0004 as a protocol rule. Pattern adopted as framing; no protocol dependency.
- **SPT (Stripe) / Agentic Tokens (Mastercard):** scoped, single-use, expiring credentials; rail-enforced limits.
- **ACP (OpenAI × Stripe):** flagship in-chat checkout rolled back within 6 months (VERIFIED press) — lesson: never depend on unshipped protocols; merchant systems of record remain the rail.
- **x402:** real but micropayment-scale (~$24M/30d).
- **Lithic:** policy backtesting against historical authorizations (P2 idea). **Skyfire:** KYA principal binding (P2 idea).
- **PulseRecover already implements** the industry-converged guardrail core: deterministic LLM-free enforcement path, per-transaction caps, velocity limits, allowlists, kill switch, non-repudiable audit intent.

## 5. The measurement critique (our sharpest edge)

- Market standard = **gross attribution**: payment succeeded after tool acted → credited to tool. Stripe's recovery definition literally includes "third-party email campaigns, in-app flows, other retry algorithms" (VERIFIED docs).
- **Redux audit (VENDOR CLAIM, disclosed dataset):** real B2C recovery 25–35% vs Stripe's 55% headline; Recurly's own data shows soft declines self-resolve at 67–68% in 2–7 days — tool-attributed recovery systematically overstates causality.
- Only FlexPay (historically) and Redux price on lift-over-baseline — both pre/post, neither randomized. **No vendor publishes a counterfactual-valid methodology.**
- **PulseRecover's standard:** gateway-confirmed capture tied to a specific action (already stricter than the market), upgraded by the P1 randomized holdout arm → gross AND incremental lift with CIs, window and denominator published. Rigorous templates: Lewis & Rao 2015 (QJE), Johnson/Lewis/Nubbemeyer 2017 *Ghost Ads* (JMR).

## 6. Benchmark ladder (context numbers, all labeled)

- Decline rates: 5–14% B2B / 6–18% B2C monthly transactions (Recurly, VERIFIED).
- Involuntary churn: 20–40% of churn (ProfitWell, VERIFIED origin); 0.45–1.32% monthly (Churnkey dataset).
- Recovery-rate ladder (self-reported, unaudited): email/dunning-only ~38–42% → processor ML retries ~55% (Stripe) → specialist vendors claim 70–89%.
- Network retry caps: Visa 15 / Mastercard 35 attempts per 30 days; fines to $15k (VENDOR-PUBLISHED via Churnkey) — encoded rationale for our stopping rules.
- India: UPI ≈83% of digital transactions (RBI); UPI technical declines <0.1–0.9% (NPCI via press), but business declines (insufficient funds, mandates) dominate and are publicly unmeasured; Stripe does not retry India-issued cards (VERIFIED Stripe docs) — the Razorpay-native lane is structurally open.

## 7. Claims discipline (what we must NOT say)

- ❌ "Razorpay can't configure retries" — the UPI Autopay Intelligent Retry Engine (beta) is merchant-configurable. Say: "classic Subscriptions stack retries are fixed-schedule, and missed cycles after `halted` are never re-attempted."
- ❌ Any recovery number without window + denominator + counterfactual basis; any "up to X%" phrasing.
- ❌ "First/only payment recovery agent" — Razorpay has announced several. Claim the *closed loop*, not the category.
- ❌ Borrowed credibility: Stripe's agent toolkit has no built-in spending limits; Spreedly's counters are placeholders; AP2/x402 are not production rails.

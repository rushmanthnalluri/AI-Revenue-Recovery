# Missing Functionality Register — PulseRecover (2026-09-02 audit)

Separated into MANDATORY (the bar requires it) / HIGH VALUE (changes the panel outcome) / OPTIONAL / DO NOT BUILD.

## What the product claims but does not actually implement

| Claim (where) | Reality | Register |
|---|---|---|
| "Autonomous revenue recovery" (README, product copy) | Auto-execute lane is structurally dead: 0/1289 ALLOWED (DEF-05); every action needs a human click | MANDATORY to fix the claim (lower floor or say "assisted") |
| "Live from Razorpay Test Mode" (Command Center subtitle) | Real merchant analytics are empty by construction — no event ingestion from sync, no detection scheduling (DEF-02) | MANDATORY (DEF-02 fix) |
| "Notifies the customer" (notify_customer action) | Both senders simulated; nothing is ever delivered (DEF-10) | HIGH VALUE — wire Razorpay link `notify.sms/email` (real, free, already the gateway's channel) |
| Policy allows 8 action types | 4 die UNSUPPORTED_ACTION at fire time (DEF-04) | MANDATORY to align (remove or implement) |
| "Scheduled reconciliation + delayed retries + notifications" (worker tier) | Shipped and ticking — but detection is not in the worker, so the loop still isn't closed | HIGH VALUE (part of DEF-02) |
| Webhook handlers for 6 events (docs/razorpay-integration.md) | 3 handlers; and the 3rd (`payment_link.paid`) is absent from the documented subscription (DEF-01) | MANDATORY (DEF-01 fix) |
| Subscription-aware recovery (README "landed") | Built, but undemoable — account's Subscriptions product is disabled (401) | OPTIONAL — enable product on the account if a subscription beat is wanted in the video |

## What it should implement (evidence-based)

| Item | Why (evidence) | Register |
|---|---|---|
| Payment-event derivation during sync (created/updated transitions → `payment_events`) | Unblocks detection/dashboard on real data without depending on webhook volume (DEF-02, D-REAL-1) | MANDATORY |
| Detection cadence in the worker | The only missing scheduled job; worker pattern exists (DEF-02) | MANDATORY |
| Re-anchored evaluation priors from measured outcomes | The bar demands "measured money recovered"; current priors are circular and the stored run is negative (DEF-03) | MANDATORY |
| Opt-in live-integration smoke suite (`LIVE_RAZORPAY=1`) | Both production bugs shipped under 971 green tests (DEF-06) | HIGH VALUE |
| Real customer contact via payment-link notify fields | Real delivery with zero new infrastructure (DEF-10) | HIGH VALUE |
| Verified-recovered counter on Command Center | The bar's headline metric deserves first-screen placement (Reviewer A: missing) | HIGH VALUE |
| Read-scoping or documented-open decision for GETs | Public deployment is world-readable (DEF-09) | HIGH VALUE (decision, then 1-line doc) |
| Wire `detection/run` into the Command Center UI | Dead seam today (`functionality-inventory.md`) | OPTIONAL |
| `opportunity_types` breakdown in Evaluation Lab | Persisted but invisible (DEF-23) | OPTIONAL |
| Alembic-based schema test on Postgres (docker compose) | The flagship invariant is unproven on its target DB (DEF-13) | OPTIONAL |

## Explicitly NOT missing (do not build — full rationale in do-not-build.md)

Multi-tenant merchants · distributed queue (Kafka/CELERY) · microservices · LLM-in-the-loop for the demo · generic chatbot · subscriptions product build-out beyond the shipped lane · more detectors · hash-chain external anchoring · SSO · "real-time" websocket push · mobile app.

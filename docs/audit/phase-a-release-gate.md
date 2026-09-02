# Phase A Release Gate — Verdict (2026-09-02)

Gate run against: production deployment (Render + Neon, `razorpay_test` gateway), a clean-room clone at `ecd0181`, and the full local test surface.
Evidence classes are kept separate throughout: **LIVE** = observed on the deployed stack or against the real Razorpay account · **TEST** = automated suite (993 backend tests, 9 Playwright e2e) · **CONFIG** = read back from Razorpay/Render.

## Gate scoreboard

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | A1 — real payment-link round trip to RECOVERED | **PENDING (one user card click)** | Config half done: webhook subscribes to exactly `payment.captured, payment.failed, payment_link.paid` (read back from Razorpay API, CONFIG); intake verified both directions (LIVE); registry regression test (TEST). The RECOVERED transition itself awaits a paid link (code path TEST-proven: amount/currency/fully-paid cross-check). |
| 2 | A2 — real merchant observability | **PROVEN** | Clean sqlite → real account sync: 6 payments → **6 events**; re-sync emits **0** new rows (idempotent); detection consumed **5 terminal outcomes** (LIVE). |
| 2 | A2 — production detection cadence | **PROVEN** | Worker-fired `detection.run` audit rows with `trigger: "worker"` at 11:15/11:20/11:25 UTC — 300s cadence, zero manual POSTs (LIVE). |
| 2 | A2 — one transition = one logical event | **PROVEN** | Cross-source dedupe: webhook-recorded transition is never duplicated by sync (TEST + code-reviewed path LIVE on prod: sync after payments showed 0 duplicates). |
| 3 | Source isolation ×3 | **PROVEN** | 26,253 simulator events in DB → real_test dashboard `payments_observed: 0` (no leak); research summary shows only its own data; `demo/reset` deleted exactly the research rows and left all 6 real payments untouched (LIVE). |
| 4 | Evaluation integrity | **PROVEN** | Zero hand-set conversion priors remain (code + tests: priors deleted, outcomes measured from simulator behavior); two identical canonical runs → 278 metric leaves compared, **identical except the documented 17th-digit float wobble**; lift = 0.005898 on both (LIVE local runs). Seed/dataset/model/policy/anchor all stored per run. |
| 5 | Honest presentation | **PROVEN** | Evaluation Lab (`ecd0181`): lift chip derives from stored CI — **"measured · inconclusive (underpowered)"** while CI brackets zero, "significant" only when entirely above; GROSS/VERIFIED/ACTION-ATTRIBUTED/INCREMENTAL defined separately; operational outcomes (61/100 verified actions, 98% fewer interventions, 0 unsafe) shown as their own group; stored assumptions rendered verbatim. |
| 6 | Production health | **PROVEN** | `/api/v1/system/health` reflects real subsystem state: database check actually pings Neon, worker tick age computed live ("last tick 17s ago"), policy hash read from disk, gateway mode from live config; top-level status aggregates checks (TEST-covered) (LIVE). |
| 7 | Failure rehearsal A–J | **PROVEN** (classes separated below) | A/B/E/F LIVE; C/D/G/H/I/J TEST + config-level evidence. |
| 8 | Clean startup | **PROVEN** | Fresh clone (639 files) → fresh venv → `pip install` clean → `.env.example` → alembic head `a83af82e8438` → boot → healthz/system-health ok → 50 targeted tests → real `.env` sync: **6 payments / 6 events** → `npm ci` + build + serve **HTTP 200**. No machine-specific dependency beyond the documented `.env` (LIVE). |
| 9 | Buildathon readiness re-score | below | |
| 10 | Final verdict | below | |

## Failure rehearsal (A–J) — evidence class per case

| Case | Result | Class |
|---|---|---|
| A · duplicate webhook | `200 {"status":"already_processed","duplicate":true}` — zero side effects | **LIVE** |
| B · invalid signature | `400 invalid_webhook_signature` — fail-closed | **LIVE** |
| C · API timeout | bounded httpx timeouts, GETs retried ≤3 with backoff, mutations never retried | TEST (timing assertions made load-proof) |
| D · duplicate execute | one-gateway-mutation invariant under barrier race; unique `gateway_request_id` | TEST (+LIVE in gate 1 flow when run) |
| E · payment/entity mismatch | stored `processed:false` "unknown payment; stored for reconciliation" | **LIVE** |
| F · invalid amount on `payment_link.paid` | unknown `reference_id` held, never guessed; wrong-amount hold = `verification.amount_mismatch` audit | LIVE (unknown-ref) + TEST (amount hold) |
| G · policy rejection | 1,189 BLOCKED decisions in the stored run; gate precedes every mutation | TEST + stored-run LIVE |
| H · action already terminal | idempotent no-op on already-RECOVERED | TEST |
| I · missing credentials | simulator fallback + typed `razorpay_not_configured` 409 on execute | TEST (+LIVE earlier this week) |
| J · invalid credentials | probe → `authentication_failed`; subscriptions 401 degraded per-entity | TEST + **LIVE** (the real 401 incident) |

## Buildathon readiness re-score (Track 03 bar)

| Requirement | Verdict | Evidence |
|---|---|---|
| Detects revenue at risk | **PROVEN** | worker cadence + sync-derived events feed the detector (LIVE) |
| Determines the intervention | **PROVEN** | diagnosis RF served (prod-frame top-1 0.7154, frame-qualified) + strategy generation + policy gate (TEST/LIVE) |
| Executes a bounded recovery workflow | **PROVEN (config) / PENDING (live fire)** | gate + exactly-once executor + approval lane (TEST); live RECOVERED awaits gate 1's click |
| Measured money recovered | **PROVEN (honest)** | +0.59 pp ITT [−0.9, +1.5] — measured, CI-brackets-zero, **labeled inconclusive**; operational metrics separate (LIVE runs ×2) |
| Compliant escalation | **PROVEN** | PENDING_APPROVAL lane, reason-required reject/escalate, KYA-lite binding (TEST/LIVE) |
| Stopping rules | **PROVEN** | cooldowns, duplicate guard, never-retry-mutations, UNKNOWN lane, approval TTL available (TEST) |
| Audit trail | **PROVEN** | hash-chained, `CHAIN VALID` on prod, public verify endpoint (LIVE) |
| Failure handled gracefully | **PROVEN** | two real production incidents absorbed by design + rehearsal table above |

## Defect verdicts

- **DEF-01 (webhook subscription): PROVEN** at config + intake + registry level (CONFIG/LIVE/TEST). The last inch — a paid link flipping a real action to RECOVERED — is gate 1, pending one card click; the transition logic itself is TEST-proven including amount/currency/partial-payment holds.
- **DEF-02 (real merchant loop): PROVEN.** `Razorpay → sync → payment_events → 300s detection → dashboard` is live on prod; idempotency and cross-source dedupe verified.
- **DEF-03 (evaluation): PROVEN as integrity fix.** Priors eliminated; outcome model measured; result `+0.59 pp [CI crosses 0]` published and labeled **inconclusive** — deliberately not tuned.
- **Real merchant loop: PROVEN.** **Synthetic contamination: PROVEN NONE.** **Real Razorpay recovery: PENDING gate 1's click** (all machinery live-verified except the final paid-link transition).

## FINAL VERDICT

**NO-GO today → GO the moment gate 1 completes.** Exactly one remaining blocker: a real payment on a PulseRecover-created recovery link, watched through `payment_link.paid` to RECOVERED. Every other gate item is PROVEN with class-separated evidence.

## What remains after gate 1 (P1/P2, not blockers)

DEF-04 allowlist↔executor alignment · DEF-05 auto-lane decision · DEF-06 live smoke suite · DEF-07 Makefile de-hardcoding · DEF-09 open-GETs decision · DEF-10 notify via Razorpay link notify fields · DEF-11 docs drift sweep · DEF-12 frame-qualified ML numbers in narration · DEF-13 alembic/Postgres schema-fidelity test · DEF-14 UI fixes (verify-strip, table overflow, h2/h3) · keep-warm ping for the demo window.

# Defect Register — PulseRecover (2026-09-02 audit)

Priorities: **P0 = submission blocker** (breaks the brief's bar or the demo's spine) · **P1 = high-value fix** · **P2 = worthwhile** · **P3 = optional polish**. Every defect cites evidence; nothing here is opinion-only.

## P0 — submission blockers

| ID | Title | Evidence | Root cause | User/judge impact | Fix complexity | Recommended fix |
|---|---|---|---|---|---|---|
| DEF-01 | Webhook subscription docs omit `payment_link.paid` — live recovery verification cannot fire | registry = 3 handlers incl. `payment_link.paid` (`webhook_handlers.py:233-237`); docs/render.yaml/dashboard list 6 events without it (`razorpay-audit.md`) | Docs written for 6 events, registry implemented for 3; never reconciled | A paid recovery link never marks RECOVERED via webhook; demo climax (verified recovery) fails live; actions park VERIFYING | S (docs + render.yaml + dashboard config) | Subscription = `payment.captured, payment.failed, payment_link.paid`; fix docs/razorpay-integration.md handler list (6→3); re-configure dashboard; prove one live RECOVERED |
| DEF-02 | real_test closed loop never closes: detection is manual-only and its event stream is webhook-only | `run_detection` callers = API/demo/eval only (grep); worker never detects (`worker.py:103-138`); sole PaymentEvent writer `webhook_handlers.py:298-309`; live: 6 payments, `payments_observed: 0` | REST sync upserts entities but derives no events; no detection scheduler | The "live from Razorpay Test Mode" console is empty at the exact moment a judge opens it; detection/diagnosis/recovery never trigger on real data | M | (a) worker tick runs detection per environment on cadence; (b) sync derives payment_events from observed state transitions (created/updated captures); both keep Research Lab untouched |
| DEF-03 | The only stored evaluation argues against the product: holdout lift **−2.55 pp** | live `/api/v1/evaluation/metrics`; `flows-recovery-evaluation.md` §I; conversion priors hand-set (`runner.py:130-173`) | Priors are circular (assumed per-action conversion), and the gate routes 94% to approval (no auto execution in-harness) | A judge who opens Evaluation Lab sees the product losing to naive retry by 59× | M | Re-anchor priors from measured fleet outcomes (or simulator ground-truth conversion), re-run, publish honest number; if lift stays negative, reframe the headline metric (recovered ₹ per intervention + zero-unsafe) and say why |

## P1 — high-value fixes

| ID | Title | Evidence | Root cause | Impact | Fix complexity | Fix |
|---|---|---|---|---|---|---|
| DEF-04 | 4 of 8 allowlisted action types die as UNSUPPORTED_ACTION on fire | `executor.py:923-928`; `flows-recovery-evaluation.md` #4 | Policy allowlist outran executor mappings | A policy-APPROVED action can fail at fire time — worst kind of inconsistency | S | Remove from allowlist (honest) or implement mappings |
| DEF-05 | Auto-execute lane structurally dead: 0 ALLOWED / 1289 gate decisions | `strategies.py:45` (cap 0.80 < floor 0.85); stored run stats | Diagnosis-free confidence capped below the auto floor | "Autonomous" claims are false today; every recovery needs a click | S | Either lower floor for bounded actions or declare assisted-by-design in copy |
| DEF-06 | Test suite cannot see a real-integration break | conftest pins blank keys (`tests/conftest.py:17-23`); 100% MockTransport; `test_real_data_workflow.py:447` patches and asserts the patch | No opt-in live marker; manual-only verification | 971 green tests are consistent with a broken live path (proven: the subscriptions-401 and webhook-list bugs both shipped green) | M | Opt-in `LIVE_RAZORPAY=1` smoke suite (probe + one sync page + signed webhook) + rename the mock file honestly |
| DEF-07 | `make setup` fails off this machine; e2e config machine-shaped | `Makefile:10` hardcodes `/c/Users/rushm/...`; `playwright.config.ts:41` Windows venv path | Developer paths committed | Judge's first clone-and-run fails (public repo submission) | S | Use `python`/`py` detection; parameterize e2e config |
| DEF-08 | Command Center contradicts Payments (0 records vs 6 payments) | live UI (`ui-review.md` HIGH-1); `dashboard.py:156-167` event-window reads | DEF-02's starvation surfacing in UX | First impression says "product sees nothing" while data exists | S (follows DEF-02) or interim honest copy | Fixed by DEF-02; else banner explaining the event stream |
| DEF-09 | All GET endpoints world-readable on a public deployment | live probe: `/api/v1/payments` 200 unauthenticated (`security-audit.md` #1) | Auth guards mutating methods only (documented demo choice) | Anyone with the URL reads merchant-shaped data + audit trail | S | Decide: document as demo-judge choice, or add read-scope to the shared key |
| DEF-10 | `notify_customer` marks SENT, delivers nothing | `worker/senders.py:29-75` (both senders simulated) | No real channel wired | Recovery's customer contact is fiction; a judge asking "did the customer get anything?" gets no | S | Use Razorpay payment-link `notify.sms/email` fields — a REAL delivery channel the gateway already owns |

## P2 — worthwhile improvements

| ID | Title | Evidence | Impact | Fix complexity |
|---|---|---|---|---|
| DEF-11 | architecture.md predates 3 packages; table count 21→24; "no scheduler" stale; security-testing test counts stale (107/98 vs 93 actual); README/claim-matrix test count 678 vs 971 | `architecture-actual.md` register; `test-coverage.md` drift table | Doc drift | Judges reading docs find contradictions with code | S |
| DEF-12 | Served diagnosis model is 0.7154 top-1 on prod frames; exp07 ship = disclosed override of a failed pre-registered clause | `ml-audit.md` MEDIUMs; `exp07/metrics_v4b2.json` | Headline blocks (0.91/0.995) are exact-span readings | Misleading if quoted bare in the video | S (frame-qualify in docs/narration) |
| DEF-13 | Tests build schema via `create_all`, not alembic; FOR UPDATE is a silent no-op on SQLite — the double-fire invariant is unproven on its target DB (Postgres) | `test-coverage.md` MEDIUMs | SQLite-only test schema path | Postgres drift invisible; the flagship invariant rests on a 3-run manual Postgres check | M |
| DEF-14 | UI defects: audit-verify strip collapses header (~82px column); payments table 1160px overflow, clipped at 1440px, 3.6× scroll on mobile; zero h2/h3 anywhere; "1 events" plural | `ui-review.md` MEDIUM-1/2/3, LOWs | Polish gap on an otherwise strong UI | S each |
| DEF-15 | Diagnosis feature loading is the one environment-unscoped reader | `features.py:144-149` (`flows-detection-diagnosis-revenue.md` #3) | Latent cross-env contamination seam | S |
| DEF-16 | `incidents.revenue_at_risk_paise` morphs methodology between list and detail | `dashboard.py:102-125` (`data-reality.md` D-REAL-3) | Confusing money numbers | S (label the basis in API/UI) |
| DEF-17 | Invalid policy file + execute → opaque 500 instead of clean 4xx | `security-audit.md` #8 (`executor.py:173`) | Edge-case error honesty | S |
| DEF-18 | Free-tier cold starts produce 10s-timeout error panels on first visit | live screenshots; `ui-review.md` | First-hit judge experience stutters | S (keep-warm ping or paid tier; banner retry already exists) |

## P3 — optional polish

| ID | Title | Note |
|---|---|---|
| DEF-19 | Rate limiting per-process, skips GETs | Fine at one instance |
| DEF-20 | Simulator default webhook secret in source | Documented accepted risk; sim-only |
| DEF-21 | Dead UI seams: no caller for `recovery/cancel`, `evaluation/metrics`, `detection/run` | Wire `detection/run` into Command Center once DEF-02 lands |
| DEF-22 | `_demo` scenario ids violate the project's own label rule | Trivial copy fix |
| DEF-23 | approvals totals exist but `opportunity_types` breakdown invisible in UI | Wire the persisted field into Evaluation Lab |

# Prioritized Roadmap — PulseRecover to Buildathon-ready

Deadline context: Sept 5, 2026 (third-party date, ~3 days). Phases are ordered by submission impact per engineering hour. Every task lists files, dependencies, risk, verification, and buildathon value. No generic items — each traces to a defect (DEF-xx) in `defect-register.md`.

## PHASE A — Critical correctness (Day 1, must all land)

| ID | Task | Files/services | Depends | Risk | Verification | Bar value |
|---|---|---|---|---|---|---|
| A1 | Fix webhook subscription everywhere: `payment.captured, payment.failed, payment_link.paid`; correct handler list (6→3) in docs | `docs/razorpay-integration.md`, `render.yaml` comment, Razorpay dashboard config | — | none | Signed live probe of each type; dashboard delivery log shows 200s | DEF-01 — verified recovery can fire |
| A2 | Derive `payment_events` from sync state transitions (new/updated captured/failed entities), environment-stamped, idempotent | `services/merchant/service.py` (+ tests) | — | low (additive) | New sync on prod emits events; re-sync does not duplicate (idempotency test) | DEF-02 — real analytics wake up |
| A3 | Worker detection cadence: per-environment `run_detection` in the tick, config-gated like reconcile | `services/worker/worker.py`, `config.py` (+ tests) | A2 | low | Worker test: detection fires on cadence; prod health shows incidents after next burst | DEF-02 — loop closes autonomously |
| A4 | Live RECOVERED round-trip proof: execute one link recovery on prod, pay it, watch `payment_link.paid` → RECOVERED + audit chain | prod only | A1 | low | DB shows RECOVERED; `/api/v1/audit/verify` VALID; screenshot for the video | The demo's climax exists |
| A5 | Kill the contradiction: Command Center honest copy or populated surface | `command-center-screen.tsx` | A2/A3 | none | Home shows records; Payments and home agree | DEF-08 |

## PHASE B — Real Razorpay integration (Day 1–2)

| ID | Task | Files | Depends | Risk | Verification | Value |
|---|---|---|---|---|---|---|
| B1 | Align policy allowlist ↔ executor: remove the 4 unimplemented types (or implement them) | `policies/default.yaml` NOTE: hash changes — check pinned-hash tests first; `executor.py` | — | medium (policy version bump) | Suite green; allowlist ⊆ executor map test | DEF-04 |
| B2 | Real customer contact: `notify.{sms,email}` on recovery payment links + honest sender naming | `services/razorpay/client.py`, `worker/senders.py` | — | low | Paid link shows Razorpay-side notification; outbox row truthful | DEF-10 |
| B3 | Auto-lane decision: either bounded auto-execute (floor ≤ capped confidence for low-risk actions) or "assisted by design" copy | `policies/default.yaml` or frontend copy | B1 | low | Gate decision distribution changes, or copy audit passes | DEF-05 |

## PHASE C — Data integrity (Day 2)

| ID | Task | Files | Depends | Risk | Verification | Value |
|---|---|---|---|---|---|---|
| C1 | Re-anchor evaluation conversion priors from measured fleet outcomes (holdout A/B within simulator truth); re-run standard preset; store the honest run | `services/evaluation/runner.py`, `revenue/config.py` | — | medium (numbers may still be low — accept honesty) | New stored run with CI; methodology note | DEF-03 — the bar's core metric |
| C2 | Verified-recovered counter on Command Center (sum of webhook-verified RECOVERED amounts, environment-scoped) | `api/v1/dashboard.py`, `command-center-screen.tsx` | A4 | low | Counter shows the A4 recovery ₹ | Reviewer A "missing" |
| C3 | Scope the last unscoped reader (diagnosis `load_window_records`) | `features.py` | — | low | New regression test like the revenue/insights ones | DEF-15 |

## PHASE D — AI/ML quality (Day 2, cheap honesty only)

| ID | Task | Files | Verification | Value |
|---|---|---|---|---|
| D1 | Frame-qualify every quoted diagnosis number (docs + video script): prod-frame top-1 0.7154 as THE number | `docs/ml.md`, README, demo script | No bare 0.91/0.995 anywhere | DEF-12 |
| D2 | Do NOT retrain or re-anchor models before submission | — | — | Avoids gate-semantics churn (`ml-audit.md`) |

## PHASE E — Safety/reliability (Day 2)

| ID | Task | Files | Verification | Value |
|---|---|---|---|---|
| E1 | Opt-in live smoke suite: `LIVE_RAZORPAY=1` → probe + one sync page + signed webhook round-trip; rename `test_real_data_workflow.py` honestly | `tests/razorpay/test_live_smoke.py` (new, skipped by default) | Runs green against the test account on demand | DEF-06 |
| E2 | Open-GETs decision: document as judge-access choice OR add read scope to the shared key | `main.py` + README | One-line decision recorded | DEF-09 |
| E3 | Invalid-policy-file → clean 4xx on execute | `executor.py` | New test | DEF-17 |
| E4 | Keep-warm ping for the demo window (UptimeRobot 5-min on /healthz) | ops | No cold-start during the video | DEF-18 |

## PHASE F — UX (Day 2–3)

| ID | Task | Files | Verification | Value |
|---|---|---|---|---|
| F1 | Fix audit-verify strip collapse | `section-card.tsx` / `audit-verify-action.tsx` | Screenshot same viewport | DEF-14 |
| F2 | Payments table: horizontal scroll affordance or responsive columns | `payments-view.tsx`, `data-table.tsx` | 1440px + 390px screenshots | DEF-14 |
| F3 | Heading hierarchy h2/h3 per page | page components | a11y probe | DEF-14 |
| F4 | Misc copy: "1 events", `_demo` labels, verify-scope note | assorted | grep | DEF-22 |

## PHASE G — Evaluation presentation (Day 3)

| ID | Task | Verification | Value |
|---|---|---|---|
| G1 | Wire `opportunity_types` breakdown into Evaluation Lab | Visible per-run split | DEF-23 |
| G2 | If C1 lift is still ≤0: present recovery-per-intervention + zero-unsafe + coverage explicitly as the operating point, with the naive baseline's intervention count beside it | Methodology panel reads coherently | Turns DEF-03 into a safety story with numbers |

## PHASE H — Deployment/repro (Day 3)

| ID | Task | Files | Verification | Value |
|---|---|---|---|---|
| H1 | De-hardcode Makefile (`python` detection) + e2e config paths | `Makefile`, `playwright.config.ts`, `e2e/stack.ts` | Fresh clone `make setup` works | DEF-07 |
| H2 | Docs refresh sweep: architecture.md (+3 packages, 24 tables, worker), security-testing counts, README test count 971, data-flow scheduler note | 4 docs | architecture-actual.md register cleared | DEF-11 |

## PHASE I — Submission packaging (Day 3–4)

| ID | Task | Verification | Value |
|---|---|---|---|
| I1 | Record the 5-minute video per `demo-plan.md` (two rehearsal runs first) | Final cut ≤5:00, all beats live | The actual submission |
| I2 | Architecture one-pager refresh (link `architecture-actual.md` content) | Panel reads current truth | Submission |
| I3 | Form answers: "What broke and how you got out" ← the two real incidents (subscriptions-401, webhook secret mismatch) are the answer, with commit refs | Written, honest, evidence-linked | Differentiating |

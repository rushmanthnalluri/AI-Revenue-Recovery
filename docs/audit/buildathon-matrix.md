# Buildathon Rubric Matrix — PulseRecover vs Track 03 (AI Revenue Recovery)

Brief source: razorpay.com/buildathon (VERIFIED live 2026-09-02, `docs/audit/buildathon-brief-evidence.md`).
Submission mechanics (official): **public repo + 5-minute pitch video + the architecture** → panel. No weighted rubric published; the per-track "bar" is the only official judging language. Third-party axes (problem taste / build quality / AI judgment / failure recovery) are UNVERIFIED but consistent — used as secondary columns only.
**Deadline: September 5, 2026 (third-party-only, officially silent) — treat as ~3 days.**

## The official bar, verbatim

> "Build an agent that **detects revenue at risk**, **determines the right intervention**, and **executes a bounded recovery workflow**: from payment failures and checkout abandonment to overdue receivables."
> "Don't just identify the problem. **Show measured money recovered across a batch**, with **compliant escalation**, **stopping rules**, and an **audit trail**."

## Requirement-by-requirement mapping

| Requirement (from the bar) | Current implementation | Evidence | Working? | Gap | Priority | Recommended fix |
|---|---|---|---|---|---|---|
| Detects revenue at risk | v2 detection engine over payment-event stream; floors/dedup/cooldowns; opt-in baselines | `services/detection/*`; 0.698 F1 stored run | **PARTIALLY_WORKING** | Manual-trigger only; real_test event stream starved (webhook-only writer); UI shows "No payment activity" with 6 payments present | **P0** | Worker-driven detection cadence + derive payment_events during sync upserts; then real_test detects autonomously |
| Determines the right intervention | Diagnosis RF model (prod-frame top-1 0.7154) + strategy generation + policy gate | `services/diagnosis`, `services/recovery/strategies.py` | WORKING | Headline numbers need frame qualification; heuristic fallback weak on prod frames (0.39–0.46) | P2 | Frame-qualify metrics everywhere; keep model |
| Executes a bounded recovery workflow | Policy gate (confidence floor, amount caps, cooldowns, kill switches) + exactly-once executor + approval lane + UNKNOWN re-query lane | `services/policy`, `services/recovery/executor.py`; 93 security tests | WORKING | 4/8 allowlisted action types unimplemented in executor; auto lane structurally dead (0/1289 ALLOWED) | P1 | Align allowlist↔executor; decide auto-lane floor or "assisted by design" messaging |
| Payment failures (direction 1) | failed_payment_retry via payment links — **live-proven on Razorpay Test Mode** | dcef95a sync; 7 links created; `webhook_handlers.py:236` | WORKING | Recovery **verification** broken live: `payment_link.paid` absent from webhook subscription (docs+dashboard) | **P0** | Fix subscription list (docs, render.yaml, dashboard); prove a live RECOVERED round-trip |
| Checkout abandonment (direction 2) | stuck_checkout_payment + dropped_checkout opportunity types; 2 unpaid orders seeded live | `builder.py` | PARTIALLY_WORKING | Recovery-link verification gap (same P0); no real volume yet | P1 | Same P0 fix + seed more abandoned checkouts |
| Overdue receivables / failed subscriptions (direction 3) | subscription_halted/pending opportunities shipped (builder+strategies) | wave-1 feature; `models/commerce.py` Subscription | **UNIMPLEMENTED in practice** | Account's Subscriptions product disabled (401); executor can't pause/resume | P2 | Enable product on account to demo, or de-emphasize in submission narrative |
| **Measured money recovered across a batch** | Randomized-holdout evaluation harness with ITT lift + CI | `services/evaluation/runner.py`; stored run | **BROKEN (against us)** | The one stored run shows **negative lift −2.55 pp** (gated loop 0.46% vs naive 27%); conversion priors are hand-set (circular) | **P0** | Re-anchor priors from measured fleet outcomes; re-run; publish honest number or reframe metric (recovery per intervention + zero-harm) |
| Compliant escalation | PENDING_APPROVAL lane, escalation with reason, KYA-lite principal binding, SoD warning | `api/v1/recovery.py`, wave-1 KYA | WORKING | Approver identity still shared-key cohort | P3 | Acceptable for demo; note in architecture |
| Stopping rules | Policy cooldowns, duplicate guard, approval TTL (opt-in), never-retry-mutations, UNKNOWN lane | `policies/default.yaml`, executor | WORKING | TTL disabled by default (documented) | P3 | Leave as-is; mention availability |
| Audit trail | Append-only AuditLog, **hash-chained + public verify endpoint** | `models/system.py`, `services/audit/verify.py`; chain VALID live | WORKING | Tamper-evident not tamper-proof (documented) | — | None needed |
| Public repo | github.com/rushmanthnalluri/AI-Revenue-Recovery | live | WORKING | `make setup` fails off-machine (hardcoded dev path) | P1 | Fix Makefile + e2e config paths |
| 5-minute pitch video | — | — | **MISSING** | Needs the working demo first | P0 | `demo-plan.md` (this audit) |
| The architecture | docs/architecture.md (+ this audit's architecture-actual.md) | — | PARTIALLY_WORKING | architecture.md predates merchant/worker/audit packages; 21→24 tables | P2 | Refresh doc (or link audit file) |

## Secondary (unverified) judging axes

| Axis | Score the panel can reach today | Main blocker |
|---|---|---|
| Problem taste | High — correct track, differentiated lane (bounded execution + verified recovery, not another dashboard) | — |
| Build quality | High — 971 tests, contract-maintained API, enforced architecture boundaries | Suite can't see real-integration breaks (P1) |
| AI judgment | Medium — heuristic reasoner in prod; honest guardrails; but "AI" in prod is modest and the eval shows negative lift | EVAL re-anchor (P0) |
| Failure recovery | High — the two real production incidents (subscriptions-401, webhook secret mismatch) were absorbed exactly as designed, and the audit documents them | — |

## Verdict

Track 03 fit is **real and strong** — but three P0s stand between the current state and the bar: (1) the webhook subscription bug that blocks *verified* recovery live, (2) the starved real_test event stream that makes the merchant story look empty, (3) an evaluation artifact that currently argues **against** the product thesis. All three are fixable inside the deadline window. Everything else is polish.

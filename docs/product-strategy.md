# PulseRecover — Product Strategy

**Author:** Research + Product Strategy Lead · **Date:** 2026-08-27 · **Status:** Converged (loop exit criteria met)

This document is the output of the strategy loop: repo audit → external research → approach scoring → selection → red-team → refinement. Inputs: `docs/research.md` (2026-08-26 + 2026-08-27 refresh), `docs/competitive-analysis.md`, and an independent full-repo audit (2026-08-27, summarized in `docs/decision-log.md` D15).

---

## 1. The problem (verified)

- **Payment failures are large and mostly un-recovered.** Card decline rates: 5–14% of monthly transactions B2B, 6–18% B2C (Recurly network data, VERIFIED). Involuntary churn is 20–40% of total churn (ProfitWell, VERIFIED origin). Razorpay's own blog states ~33% of failed transactions are never re-attempted (VENDOR CLAIM, `docs/research.md`).
- **Failures cluster into incidents, but tools treat them as independent transactions.** Gateway degradations, bank downtimes, method outages, and latency spikes produce *correlated* failure bursts. Every dunning vendor starts after an individual payment fails; none asks "is there a degradation event, where, why, what is it worth."
- **Recovery claims are methodologically soft.** The market standard is gross attribution ("payment succeeded sometime after we acted"). Stripe's own definition counts recovery "by any means" (VERIFIED). Redux's audit puts real B2C recovery at 25–35% vs Stripe's 55% headline (VENDOR CLAIM with disclosed dataset).
- **Track 03's bar** (VERIFIED, razorpay.com/buildathon, 2026-08-27): *"Don't just identify the problem. Show measured money recovered across a batch, with compliant escalation, stopping rules, and an audit trail."*

**Problem statement:** When a merchant's payment performance degrades, nobody detects it as an incident, explains why, prices the damage, acts within explicit financial guardrails, and *proves* whether the action recovered money.

## 2. Defensible differentiation

**The open lane (confirmed twice, 2026-08-26 and 2026-08-27):** merchant-side degradation **detection → root-cause diagnosis → revenue-at-risk → policy-gated bounded execution → webhook-verified outcome → measured recovered revenue**. Nobody occupies it:

- Razorpay's announced agents (Abandoned Cart, Dispute Responder, Subscription Recovery, Cashflow Forecaster, Receivables Agent) are outreach/collections agents — none publishes detection, diagnosis, or verification semantics.
- Dunning vendors (Stripe, Adyen, Chargebee, Recurly, Butter, FlexPay/Revaly, Redux, Churnkey, Baremetrics) do per-transaction retry orchestration. Zero do incident detection (Pagos excepted — but it delivers daily/weekly files, no execution), zero do diagnosis, zero do policy gating, zero do causal verification.
- AIOps (Datadog, PagerDuty, Dynatrace) detects and diagnoses but knows nothing about money; Dynatrace's "approved actions under policy guardrails" independently validates our policy-gate design.

**Two lanes are now CLOSED to us** (Razorpay-native occupants announced): Hinglish voice recovery (Sarvam partnership + voice-led Subscription Recovery Agent) and B2B receivables chasing (RazorpayX Receivables Agent). We do not pitch either as core.

**Our sharpest edge:** a counterfactual-honest measurement story. With a randomized holdout we can report *incremental lift with confidence intervals* — exceeding the published rigor of every vendor in the market (all gross attribution; only FlexPay/Redux attempt pre/post lift, neither randomized).

## 3. Approaches considered and scored

Scale 1–5. Criteria: Razorpay relevance, technical depth, measurable business value, AI usefulness, feasibility, implementation risk (5 = low risk), demo strength, hiring signal.

| Approach | RZ rel | Depth | Value | AI use | Feasib. | Risk | Demo | Hiring | **Total /40** |
|---|---|---|---|---|---|---|---|---|---|
| **A. Sharpen the closed-loop platform** (fix credibility, add holdout evaluation + decline-outlier diagnostics + mandate framing) | 5 | 5 | 5 | 4 | 5 | 5 | 5 | 5 | **39** |
| B. Pivot to subscription-arrears specialist (halted-sub payment links) | 4 | 3 | 3 | 2 | 4 | 4 | 3 | 3 | **26** |
| C. Hinglish voice recovery / B2B receivables | 3 | 3 | 4 | 4 | 2 | 1 | 4 | 2 | **23** |
| D. Payment observability only (Pagos-style, no execution) | 3 | 3 | 3 | 2 | 4 | 4 | 3 | 3 | **25** |
| E. Reposition as recovery-measurement product (LiftLab) | 4 | 4 | 3 | 2 | 3 | 3 | 3 | 4 | **26** |

**Rejected reasoning:** B wastes the detection/diagnosis/evaluation investment and abuts Razorpay's Subscription Recovery Agent. C competes with the sponsor's announced roadmap. D violates the track bar (no bounded workflow execution, no measured recovery). E is a *component* of A, not a product — measurement alone executes nothing.

## 4. Selected approach: A — sharpen the closed loop

Keep the built product. Three surgical additions, each small and leveraged on existing structures, plus a credibility pass:

1. **Randomized holdout evaluation arm.** Add a third arm to the existing harness: 5–10% of failed payments (randomized at *customer* level) receive no PulseRecover action. Pre-registered estimand: incremental lift = recovery_rate(treatment) − recovery_rate(control) over a fixed attribution window; report gross AND incremental with CIs; stratify by failure class/method; survival analysis of time-to-recovery. **This is the single highest-leverage credibility move — the entire market is one RCT behind.**
2. **Decline-outlier diagnostics.** On every incident, rank bank/method/error-code facets by overrepresentation among failures (Datadog Watchdog Insights pattern — we already compute segment breakdowns, this is a re-ranking + surfacing), plus a merchant-vs-network benchmark callout (Pagos pattern: "pattern not visible platform-wide → merchant-specific"). Cheap, high diagnostic value.
3. **Mandate framing of the policy gate (AP2 patterns, zero protocol dependency).** Merchant pre-signs a constraint envelope at onboarding (our `policies/default.yaml`); the gate converts it into per-action closed authorizations; every executed action emits a receipt-like record. Honest framing: "AP2's Trusted Surface rule — the enforcement path must be deterministic, LLM-free code — is our ADR 0003/0004 stated as a protocol rule." Optionally: hash-chained audit log for non-repudiation (P2).

## 5. Red-team: challenges to the selected approach, and what changed

| Challenge | Severity | Response (strategy change) |
|---|---|---|
| "Holdout on synthetic data proves nothing — you randomized your own simulator." | High | Frame as *methodology demonstration*, which is the differentiator (vendors don't even do this). Pre-register estimand, dual-report gross + incremental with CIs, publish window/denominator. Also: harness already plays only disclosed deterministic roles. |
| "Additions = over-engineering against 'smaller exceptional product'." | Medium | Each addition is ≤1 focused work item on existing structures (evaluation arm exists; segment stats exist; policy decisions exist). Everything else in the roadmap is *deletion and fixing*, not expansion. Cut line drawn (§8). |
| "Detection precision 0.185 is the number a judge will reproduce and quote." | High | Promoted to **P1**: Watchdog-style noise floors (min-traffic gates) + per-pass incident dedup + re-detection suppression. Directly attacks the weakest published metric. |
| "Mandate framing is buzzword dressing." | Medium | Kept honest: framing + receipts only; no AP2/x402 implementation (both spec-draft/micropayment-scale; ACP's flagship was rolled back in 6 months — VERIFIED). Hash-chain demoted to P2 optional. |
| "Deadline risk (Sep 5 third-party date) — additions may not land." | Medium | P0 credibility fixes first (all <1 hour each), P1 items are independent and individually shippable; any P1/P2 can drop without breaking the whole. |
| "Approver identity is self-declared — a judge will poke the approval lane." | Medium | P1: constant-time key compare + document open-GET posture; P2: KYA-style principal binding. |

## 6. Precise MVP (what we ship = current build + P0 + P1)

**In scope (all already built, verified):** simulator with ground truth (60k+ events, 6 incident kinds); 4-detector degradation detection; ML diagnosis (held-out top-1 0.878/top-3 0.993 — the §8 LR reading at this writing; the exp07 random forest now active reads 0.910/0.995 on the same frame, `docs/ml.md` §10); heuristic-default AI investigation with tool whitelist + hallucination guard; counterfactual revenue-at-risk with CIs; strategy generation; deterministic policy gate (fail-closed, content-versioned); recovery executor (idempotent, no-blind-retry, UNKNOWN resolve); Razorpay adapter + simulated twin; webhook verification (HMAC, dedup, out-of-order safe); audit trail; baseline-vs-PulseRecover evaluation; 5 deterministic demo scenarios; console UI (6 screens) per mandated design system; docker-compose deployment.

**Added by this strategy (P1):** holdout arm + incremental-lift reporting; decline-outlier facets + merchant-vs-network callout; detection noise-floor/dedup fix; auth hardening note.

**Explicitly OUT (cut):** voice recovery, B2B receivables, AP2/x402 implementation, multi-merchant scoping, notification sending (recorded-only, disclosed), worker tier (P2), microservices, take-rate pricing framing.

## 7. Evaluation strategy

- **Detection:** precision/recall/F1/MTTD vs simulator ground truth (at this writing, pre-fix: P 0.185 / R 0.833 — the P1 fix has since shipped; before/after in `docs/detection.md`, current readings in `docs/evaluation.md` §3/§3b).
- **Diagnosis:** top-1/top-3 on held-out temporal test (0.878 / 0.993) + fresh-incident acceptance (6/6).
- **Recovery:** dual reporting — gross recovered (webhook-verified only, our existing standard, already stricter than the market) AND incremental lift vs randomized holdout with Wilson/bootstrapped CIs; stratified by failure class and method; survival analysis (time-to-recovery hazard ratio) to separate "faster" from "caused."
- **Safety invariants:** unsafe actions == 0 (asserted), stopping rules, duplicate protection — all test-proven.
- **Honesty rules:** every number carries window, denominator, and counterfactual basis; no "up to X%" phrasing; preliminary labels where applicable.

## 8. Prioritized roadmap

**P0 — credibility (< 1 day total; all diagnosed by independent audit 2026-08-27):**
1. Fix red test (`test_safe_stop.py` — parent Incident row missing under FK enforcement; caught by our own FK hardening).
2. Regenerate + commit `contracts/openapi.json` (missing `/recovery/opportunities/build` + ~24 schemas).
3. Commit the tree (single scaffold commit currently; handover risk) — *requires repo owner's git approval*.
4. Fix opportunity status drift (webhook path updates actions but not opportunity stored status; list filter vs display mismatch).
5. Delete dead code/config (`api/__init__.py:not_implemented`, `CUSTOMER_PAY_PROB` export, `pending_approval_ttl_hours`, stale `demo_run.py` docstrings, simulator config metric hints vs KNOWN_METRICS).
6. README accuracy pass (test count, contract freshness, Docker-without-artifact note, consolidated limitations).

**P1 — differentiation (independent, any may ship):**
7. Randomized holdout evaluation arm + incremental-lift reporting (§4.1, §7).
8. Decline-outlier facets + merchant-vs-network benchmark callout on incidents (§4.2).
9. Detection noise floors + per-pass dedup (precision fix).
10. Auth hardening: constant-time key compare; documented open-GET posture.

**P2 — optional polish (cut first):**
11. Mandate/receipt framing + hash-chained audit log.
12. Policy backtesting endpoint (Lithic pattern: replay policy vs last N days).
13. Worker tier: delayed-retry scheduling, UNKNOWN polling, webhook reprocessor.
14. KYA-style principal binding for approvals.

## 9. Demo story (5 minutes, deterministic)

(Numbers below are indicative at this writing (2026-08-27); the rehearsed,
verified values now live in `docs/demo.md` and `docs/demo-script.md`.)

1. **Command Center** — healthy baseline, then trigger Scenario A (demo control): success rate degrades, ₹10.4L at risk appears.
2. **Incident Intelligence** — deviation vs baseline, evidence series, decline outliers, ML diagnosis (gateway_degradation, confidence 0.90).
3. **AI Investigation** — observed facts vs AI inference vs recommended action, clearly separated; policy preview.
4. **Recovery Planner** — strategy comparison; auto-execute a ₹504 retry (ALLOWED, conf 0.88) → webhook-verified RECOVERED in seconds.
5. **Approval Center** — ₹8,047 action held for approval (rules shown); approve → executes → verified.
6. **Failure proof (Scenario E/D)** — AI proposes refund → POLICY BLOCKED, zero gateway calls; gateway timeout → UNKNOWN, no duplicate, GET-only resolve.
7. **Evaluation Lab** — baseline vs PulseRecover: 98% fewer interventions, 0 unsafe; gross + incremental-lift numbers with CIs; the honest precision trade-off, stated.
8. **Audit Trail** — the full chain, every step traceable.

## 10. Convergence statement

Iteration 2 of the loop produced no lane changes and only sharpening deltas — the lane is confirmed open by two independent research passes (Razorpay-native + adjacent market), the build is confirmed real by an independent audit, and the additions all leverage existing structures. Further research is not producing meaningful improvements. **Loop exited.**

# Reviewer Simulation — Razorpay Hiring Panel on PulseRecover

Three simulated reviewers, using only what they could reach today: the public repo, the live deployment, and the docs.

## Reviewer A — Product

**IMPRESSIVE**
- This is a *product*, not a demo script: a merchant connects a real Razorpay account, real payments sync in with provenance, and recovery ends in a **real Razorpay payment link** — not a log line saying "would retry".
- The honesty layer is unlike typical buildathon submissions: provenance chips everywhere, "Synthetic Research Dataset" vs "Razorpay Test Mode" labels, failure disclosures in the README, a quarantine ledger for bad gateway rows.
- Environment separation (Real Merchant vs Research Lab) is a product-level idea competitors don't show — synthetic scale for evaluation without contaminating the merchant story.

**CONCERNING**
- The first screen says "No payment activity yet" while Payments lists 6 real payments — the merchant narrative looks broken on arrival (DEF-02/08).
- The only numbers a visitor can find (Evaluation Lab) show the product recovering **less than doing nothing** (DEF-03). If the panel opens it before we explain, the thesis is dead on arrival.
- "Autonomous recovery" language vs every action needing a human click (DEF-05).

**MISSING**
- A visible "money recovered" counter that is *verified*, not estimated. The one number the bar asks for ("measured money recovered") is not on the first screen.
- Any real customer contact — notifications are simulated (DEF-10).

**CONFUSING**
- When is data real? (Labels exist, but the empty real view vs full research view inverts the expected emphasis.)
- "Recoverable ₹" figures — estimated or measured? (They're priors; the UI doesn't say so loudly enough.)

**UNPROVEN**
- That the gated loop beats a dumb retry — the stored evidence currently says the opposite.

## Reviewer B — Engineer

**IMPRESSIVE**
- The money-movement core is the strongest buildathon-grade work I've seen: exactly-once mutations with ledger idempotency, transient→UNKNOWN→GET-only re-query (never blind-retry), duplicate-execute row-lock proven under a race test, amount/currency cross-check before booking RECOVERED, fail-closed webhook HMAC, hash-chained audit with a public verify endpoint.
- Architecture boundaries are *enforced by AST tests*, not convention. The agent structurally cannot reach the gateway.
- The two live incidents (subscriptions-401, webhook secret mismatch) were absorbed exactly as the failure-mode docs predicted — that rarely survives contact with production.

**CONCERNING**
- 971 green tests cannot see a real-integration break (DEF-06) — both live bugs shipped green.
- The evaluation harness's conversion priors are hand-set — the measurement is circular (DEF-03).
- Policy allows 4 action types the executor can't perform (DEF-04).
- All GETs world-readable; shared API key inlined into the public browser bundle (DEF-09; accepted demo posture, but say it out loud).

**MISSING**
- Schema-fidelity testing (alembic vs create_all; FOR UPDATE unproven on its target DB) (DEF-13).
- One real e2e against the deployed stack (Playwright runs localhost-simulator only).

**CONFUSING**
- Three GETs have write side effects (dashboard summary, incident detail auto-diagnosis, plan preview) — documented, but surprising.

**UNPROVEN**
- Postgres double-fire invariant under real concurrency (SQLite silently omits the lock).
- Diagnosis model on real merchant traffic (all metrics simulator-relative).

## Reviewer C — AI/ML

**IMPRESSIVE**
- The AI is *bounded by construction*: model proposes, deterministic policy decides, agent has no gateway access, amounts always copied from DB rows, hallucination/grounding guards strip invented numbers and targets — with prompt-injection corpus cases that actually execute zero mutations.
- Leakage discipline is real: temporal splits, time-aware calibration CV, pre-registered gates, a caught-and-kept train/held-out merge bug on record.
- The docs disclose what most teams hide: the failed NO-SHIP model, the 9/36 unsafe headlines, the frame-dependence of every metric.

**CONCERNING**
- The served model is 0.7154 top-1 on production frames; headlines quote exact-span numbers (DEF-12).
- The exp07 ship overrode a failed pre-registered continuity clause — disclosed, but a panel will probe it.
- exp08 fresh-seed top-1 = 66.7% — generalization to unseen scales is unproven (though precision at the auto floor holds).
- The heuristic fallback's "91.5%" is toy-generator-only (0.39–0.46 on prod frames).

**MISSING**
- A live LLM reasoner run (the seam exists; nothing has ever run through it in prod).
- Re-anchored evaluation with the new opt-in modes (night floors, same-time-yesterday, rescope) — they ship dark, so published anchors can't reflect them.

**CONFUSING**
- Where exactly AI ends and rules begin is well documented — but the "AI" branding oversells a prod path that is mostly deterministic heuristics + one sklearn model.

**UNPROVEN**
- Measured lift (the randomized holdout is the right design — the single stored run is negative with a CI spanning zero).

## Panel consensus (simulated)

Strong hire signal on engineering discipline, safety, and scientific honesty. The submission fails the bar **today** on three demonstrable points: verified recovery can't fire live (DEF-01), the merchant view is empty (DEF-02), and the only measurement contradicts the thesis (DEF-03). All three are fixable in days, not weeks — and fixing them converts this from "impressive repo" to "undeniable".

# Scorecard — PulseRecover as of 2026-09-02 (audit evidence-based)

Scale 0–10, no inflation. Each score cites the audit file that backs it.

| Category | Score | Justification |
|---|---|---|
| Problem quality | **9** | Track 03 verbatim fit: detection → intervention → bounded recovery across failures/abandonment/subscriptions. Real merchant pain, correctly scoped (`buildathon-matrix.md`). |
| Differentiation | **8** | Bounded execution + verified recovery + counterfactual evaluation + environment isolation — not another analytics dashboard (`reviewer-simulation.md` A). |
| Razorpay relevance | **9** | Lives entirely on Razorpay primitives: orders, payments, payment links, webhooks, subscriptions, Test Mode (`razorpay-audit.md`). |
| Real integration | **7** | Read path + payment-link write path live-proven; webhooks verified both directions. −3: subscription list bug (DEF-01), subscriptions product 401, direct payments unavailable. |
| AI depth | **6** | Genuinely bounded agent (no gateway access, grounding guards, injection corpus) — but prod runs the heuristic reasoner; LLM seam never exercised; suppression-injection uncaught (`ai-audit.md`). |
| ML depth | **6** | Real trained model served with calibration + honest experiment records and strong leakage discipline — but 0.7154 prod-frame top-1, gate override, 66.7% fresh-seed generalization (`ml-audit.md`). |
| Data quality | **7** | Real sync with provenance + idempotency proven live; quarantine ledger. −3: real event stream starved (DEF-02); only synthetic data populates analytics. |
| Measurement quality | **5** | Right design (randomized holdout, ITT, CI) — but the single stored run is negative and priors are circular (DEF-03). |
| Financial safety | **9** | Exactly-once, policy gate on every action, UNKNOWN re-query lane, amount cross-check, row-lock race proof, fail-closed HMAC (`security-audit.md`). Best-in-class for a buildathon. |
| Reliability | **7** | Worker tier, reconcile sweep, degradation paths all proven live. −3: free-tier cold starts; detection not scheduled (DEF-02). |
| Security | **6** | Money core and secrets hygiene strong. −4: world-readable GETs on a public URL, shared key in browser bundle, default `dev-key` foot-gun (`security-audit.md`). |
| Architecture | **8** | Clean enforced boundaries, port/adapter gateway, honest service conventions; some over-engineering for the stage (see do-not-build). −2 for executor/allowlist skew + doc drift. |
| API quality | **8** | Typed error envelopes, regenerated contract (42 paths), consistent pagination/provenance; three GETs with write side effects (documented). |
| Frontend UX | **7** | Exemplary provenance labeling + safety affordances + a11y chrome. −3: starved home contradicts Payments, table overflow, verify-strip collapse, no h2/h3 (`ui-review.md`). |
| Visual quality | **8** | Disciplined ops-console system; tokens match spec hex-for-hex; mobile holds up. |
| Demo quality (today) | **5** | The story as currently demonstrable: empty merchant view + a losing eval run. Fully fixable via DEF-01/02/03 (see demo-plan). |
| Documentation | **8** | Unusually honest (failure narratives, claim matrix) — but architecture/security/test-count drift and the inverted webhook handler list (DEF-11). |
| Deployment | **7** | Live full stack + blueprint + managed migrations + public repo. −3: cold starts, webhook misconfiguration shipped, Makefile dev path (DEF-07). |
| Reproducibility | **7** | Clean-room backend repro works end-to-end incl. committed model artifact (`reproduction.md`). −3: `make setup` broken off-machine; suite env-pinning means repro ≠ prod config. |
| Hiring signal | **8** | The panel simulation converges: strong-hire on discipline/safety/honesty — contingent on fixing the three P0s before the video (`reviewer-simulation.md`). |

## Overall: **7.0 / 10 today — 8.5+ achievable within the deadline window**

The gap between the two numbers is exactly three defects: **DEF-01** (webhook event), **DEF-02** (event starvation), **DEF-03** (evaluation story). All are small-to-medium engineering tasks with high certainty — not research problems. Nothing else on this card needs to change to get there.

# Do-Not-Build List — PulseRecover (2026-09-02 audit)

Each entry: what, and why building it now would actively hurt the submission.

1. **Distributed queue / Celery / Kafka for the worker tier.** The in-process worker is correct for a single-node demo and already proves the semantics (delayed retries, outbox, cadence). A queue adds deploy surface, new failure modes, and zero panel-visible value. The README already names it as post-submission work.
2. **Microservices / service split.** The monolith's boundary tests are a feature; splitting now buys nothing a judge can see and risks every invariant that matters (exactly-once, audit chain).
3. **LLM-powered reasoner for the demo.** The heuristic reasoner is deterministic, explainable, and never hallucinates on camera. Enabling `OPENAI_*` introduces a nondeterministic dependency into a 5-minute video and gains no bar points — the guardrails are already proven without it. Document the seam; don't demo it.
4. **Generic chatbot / conversational UI.** Off-brief, decoration-grade AI, and it cannibalizes video time from the measured-recovery story.
5. **More detectors / more ML models / retraining before submission.** Detection breadth is not the gap — the evaluation story is (DEF-03). The exp07 gate semantics are already dual-registered; touching models now risks the honest-numbers narrative (`ml-audit.md`).
6. **Subscriptions product build-out beyond the shipped lane.** The audit account can't even enable the product today (401). The builder/strategy lane exists and is documented; that's enough. Enabling the product on the account is a dashboard click, not engineering.
7. **External anchoring for the audit hash chain (KMS/blockchain cosplay).** Tamper-evidence with honest limits is documented and sufficient; anchors add infrastructure for a threat model the demo doesn't have.
8. **SSO / real user management / KYA-beyond-lite.** The self-declared-actor boundary is disclosed and appropriate for the panel. Neon Auth integration is the named post-submission upgrade — not now.
9. **"Real-time" websocket push / live tickers.** Fake-realtime is a judge anti-pattern; the 15–20s polling with honest timestamps is more credible.
10. **Multi-merchant tenancy.** The singleton connection is a documented scope cut; tenancy now would touch every scoped query for zero demo value.
11. **Refunds / grace-period / pause-resume executor mappings.** Not because they're worthless — because four allowlisted types that can't fire are worse than six that can (DEF-04). Remove first; implement only post-submission if a real merchant asks.
12. **Backtesting beyond the shipped endpoint, policy DSL versioning UI, metrics dashboards.** The backtest endpoint exists and is demonstrable; more tooling around it is gold-plating.
13. **Any feature whose only evidence is "competitors have it"** (`research.md` is raw material, not a shopping list). The bar is measured recovery, not feature count.

Guiding test for every proposal until Sept 5: **does a judge see it in the first 5 minutes, and does it make the measured-recovery number more true?** If no to either, it waits.

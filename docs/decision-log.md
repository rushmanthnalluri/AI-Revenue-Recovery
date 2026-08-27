# PulseRecover — Decision Log

Chronological record of material product/engineering decisions. Detailed per-decision analyses live in `docs/adr/0001`–`0009`; strategy-level decisions from the 2026-08-27 strategy round are recorded here in full. Format: **Decision** — context → alternatives → why → status.

| # | Date | Decision | Rationale (compressed) | Status |
|---|---|---|---|---|
| D1 | 2026-08-26 | Modular monolith (FastAPI), not microservices | One deployable, judge-runnable in minutes; boundaries via `ports.py` protocols | Shipped (ADR 0001) |
| D2 | 2026-08-26 | SQLite default, Postgres via compose; portable types only | Zero-dependency demo + real Postgres path (proven in containers) | Shipped (ADR 0002) |
| D3 | 2026-08-26 | Deterministic policy engine gates every financial action | "AI proposes, policy decides"; fail-closed, total, content-versioned | Shipped (ADR 0003) |
| D4 | 2026-08-26 | Reasoner is advisory-only; heuristic default, LLM optional | No keys required to demo; LLM can never execute or invent facts | Shipped (ADR 0004) |
| D5 | 2026-08-26 | Simulator with ground truth as evaluation foundation | Scientifically meaningful metrics; deterministic, resettable demo | Shipped (ADR 0005) |
| D6 | 2026-08-26 | Raw httpx for Razorpay, no SDK | Control over idempotency, retries, error mapping; fewer deps | Shipped (ADR 0006) |
| D7 | 2026-08-27 | UI design system mandated from owner's reference projects (amber-on-dark, hairlines, mono kickers) | User instruction; spec pinned in `docs/ui-design-system.md` | Shipped (ADR 0007) |
| D8 | 2026-08-26 | Opportunity-centric recovery API | Matches UI mental model; actions hang off opportunities | Shipped (ADR 0008) |
| D9 | 2026-08-26 | Synchronous evaluation harness (no worker tier for v1) | Simplicity; disclosed; 120s client timeout on long routes | Shipped (ADR 0009) |
| D10 | 2026-08-26 | Honest-metrics posture: publish unflattering numbers (detection P 0.185, baseline gross-recovery win) with trade-off analysis | Credibility with senior reviewers; numbers traceable to stored runs | Active |
| D11 | 2026-08-27 | FK enforcement on SQLite (`enable_sqlite_fk`) after a Postgres-only FK-ordering bug was found in containers | Fail fast in tests, not in production | Shipped — immediately caught a fixture bug (D15-1) |
| D12 | 2026-08-27 | Compose/env hardening: parametrized ports, JSON-form `CORS_ORIGINS`, `NEXT_PUBLIC_API_KEY` build arg | Real deployment failures found during container verification | Shipped |
| D13 | 2026-08-27 | **Stay in the closed-loop lane; avoid voice-recovery and B2B-receivables directions** | Razorpay announced native occupants (Subscription Recovery Agent + Sarvam voice; RazorpayX Receivables Agent). Closed-loop detection→diagnosis→gated execution→verified recovery remains unoccupied | Strategy (see `docs/product-strategy.md` §3–4) |
| D14 | 2026-08-27 | **Add randomized-holdout evaluation arm (incremental lift with CIs)** | Market standard is gross attribution; no vendor publishes counterfactual-valid methodology. Highest-leverage credibility move; small delta on existing harness | Roadmap P1 |
| D15 | 2026-08-27 | **Credibility-first sequencing: P0 fixes before any new differentiator** | Independent repo audit (2026-08-27) found: (1) suite red — `test_safe_stop.py` fixture missing parent Incident under new FK enforcement; (2) committed `contracts/openapi.json` stale (missing `opportunities/build` + ~24 schemas); (3) product uncommitted beyond scaffold; (4) opportunity status drift on webhook path; (5) dead code/config items; (6) demo-grade auth posture. All folded into roadmap P0 | In progress |
| D16 | 2026-08-27 | **Adopt decline-outlier facets + merchant-vs-network callouts (Watchdog Insights / Pagos patterns)** | Cheap diagnostic upgrade on existing segment stats; sharpens Incident Intelligence | Roadmap P1 |
| D17 | 2026-08-27 | **AP2-pattern framing of the policy gate (mandate envelope → closed authorization → receipt); NO protocol implementation** | AP2 is spec-draft; ACP's flagship was rolled back in 6 months. Borrow patterns, depend on nothing. Hash-chained audit demoted to P2 | Roadmap P2 |
| D18 | 2026-08-27 | **Detection precision fix prioritized (noise floors + per-pass dedup)** | 0.185 precision on scheduled passes is the weakest reproducible number; Watchdog-style min-traffic floors + re-detection suppression target it | Roadmap P1 |
| D19 | 2026-08-27 | **Claims discipline rules adopted** (no "Razorpay can't configure retries", no "up to X%", no category-first claims, no borrowed guardrail credibility) | Research refresh found counterexamples to easy attack lines | Active (see `docs/competitive-analysis.md` §7) |

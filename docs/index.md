# PulseRecover — Documentation Index

Every document in this repository, what it covers, and when to read it.
Start with the [root README](../README.md) for the product story, setup, and
headline results.

## Read first

| Doc | What it is |
|---|---|
| [architecture.md](architecture.md) | System architecture: modular monolith, ports, component + sequence diagrams, the enforced dependency matrix (ADR 0010), request traceability, money convention, safety and observability models |
| [data-flow.md](data-flow.md) | Every loop end to end: detection, AI investigation, opportunity build, execute lanes, webhook verification, UNKNOWN resolution, the reconciliation sweep (ADR 0011), demo/evaluation runs, transaction boundaries |
| [security-architecture.md](security-architecture.md) | Trust boundaries (LLM / gateway / webhook), authN-Z posture, secrets handling, financial-action safety controls, AI-specific threat mitigations, accepted residual risks |
| [security-testing.md](security-testing.md) | Adversarial break-it engagement: 13-vector attack matrix (method → result → proof test), vulnerabilities found and fixed, accepted risks, residual recommendations (`backend/tests/security/`) |
| [demo.md](demo.md) | The 5 deterministic demo scenarios: exact commands, verbatim expected outputs, and the proof suite that re-runs them |
| [demo-script.md](demo-script.md) | The 5-minute live hiring-panel runbook for the compose stack: minute-by-minute clicks/API calls, rehearsed numbers, failure beats, fallbacks, pre-flight checklist |
| [evaluation.md](evaluation.md) | Evaluation methodology and reproduced results: baseline-vs-PulseRecover harness, metric definitions, detection/diagnosis/recovery numbers, honest limitations, reproduction commands |

## Core services

| Doc | What it is |
|---|---|
| [simulator.md](simulator.md) | Deterministic synthetic payment environment: scenario presets, ground-truth recording, seeding CLI (ADR 0005) |
| [detection.md](detection.md) | Anomaly detection on the `payment_events` stream: detectors, thresholds, incident lifecycle |
| [recovery.md](recovery.md) | Recovery execution engine: opportunity building, strategy generation, the `recovery_actions` state machine, idempotency layers, failure-mode table, API surface |
| [revenue-methodology.md](revenue-methodology.md) | How revenue-at-risk and expected recovery are quantified — and why "failed transactions ≠ lost revenue" |
| [razorpay-integration.md](razorpay-integration.md) | Real adapter vs simulation twin, test-mode setup, idempotency ledger, webhook signature verification and dedup, out-of-order safety |

## AI & ML

| Doc | What it is |
|---|---|
| [agent.md](agent.md) | The AI investigation agent: heuristic reasoner (default) vs optional advisory LLM, tool whitelist, hallucination guard, report shape, guardrails |
| [ml.md](ml.md) | Diagnosis model methodology and results: 58 features, leakage controls, model comparison, final simulator-trained numbers, artifact lifecycle, retraining flow |

## Safety & policy

| Doc | What it is |
|---|---|
| [policy.md](policy.md) | The deterministic policy engine: threat model, full rule reference, configuration schema, persistence and audit-trail contracts (ADR 0003) |

## Reference

| Doc | What it is |
|---|---|
| [research.md](research.md) | Verified Razorpay API/webhook facts, existing Razorpay recovery capabilities and the overlap analysis behind the differentiation strategy, buildathon intelligence |
| [ui-design-system.md](ui-design-system.md) | Binding frontend design spec: tokens, component recipes, motion, do/don't — mandated from the user's reference projects (ADR 0007) |

## Architecture decision records (`adr/`)

| ADR | Decision |
|---|---|
| [0000](adr/0000-template.md) | Template (Decision / Context / Options / Chosen / Why / Tradeoffs) |
| [0001](adr/0001-modular-monolith.md) | Modular monolith over microservices |
| [0002](adr/0002-sqlite-default-postgres-compose.md) | SQLite by default, Postgres via docker compose |
| [0003](adr/0003-deterministic-policy-gate.md) | Deterministic policy engine gates all financial actions |
| [0004](adr/0004-reasoner-advisory-llm-optional.md) | Reasoner never executes; LLM optional, heuristic default |
| [0005](adr/0005-simulator-ground-truth.md) | Simulator with ground truth for scientific evaluation |
| [0006](adr/0006-raw-httpx-no-razorpay-sdk.md) | Raw httpx REST client instead of the Razorpay SDK |
| [0007](adr/0007-frontend-design-system-mandated.md) | Frontend design system mandated from the user's reference projects |
| [0008](adr/0008-opportunity-centric-recovery-api.md) | Opportunity-centric recovery API (per-payment, not batched) |
| [0009](adr/0009-synchronous-evaluation-harness.md) | Synchronous evaluation harness (blocking CLI/API, no job queue) |
| [0010](adr/0010-dependency-direction-enforcement.md) | Dependency direction enforced by AST-based architecture tests |
| [0011](adr/0011-operator-triggered-reconciliation.md) | Operator-triggered reconciliation sweep (no scheduler; worker tier P2) |

## Suggested reading paths

- **Judge / reviewer (10 min):** README → demo.md → evaluation.md §2 →
  policy.md §1. **Live panel:** demo-script.md.
- **Backend engineer:** architecture.md → ports + the service doc for your
  area → policy.md → razorpay-integration.md.
- **ML engineer:** ml.md → simulator.md → evaluation.md.
- **Frontend engineer:** ui-design-system.md → architecture.md §1 →
  frontend/README.md (in `frontend/`).

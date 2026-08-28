# PulseRecover — Release Readiness

**Status: FROZEN.** Feature development is stopped. This document is the final
evidence pack for the Razorpay AI Buildathon (Track 03) submission.
Prepared 2026-08-28. Release-candidate base: commit `220cc9a`; the freeze
commit is the commit that adds this file (see git log).

> Reading rules: every number below carries its run id / test / doc source.
> "Verified recovered" means webhook/inline-confirmed `RECOVERED` only.
> "Simulation" means the labeled simulator modeled on documented Razorpay API
> semantics and test-mode behaviors — not Razorpay infrastructure.

---

## 1. What PulseRecover is

An AI payment reliability and revenue recovery engine with one closed loop:
payment events → anomaly detection → incident → ML diagnosis → AI
investigation → counterfactual revenue-at-risk → strategy generation →
deterministic policy gate → bounded execution via Razorpay (Test Mode) or a
clearly-labeled simulated gateway → webhook verification → measured recovered
revenue. Governing principle (ADR 0003/0004): **probabilistic AI proposes;
deterministic policy decides; payment infrastructure executes; verification
proves.** The dependency direction is enforced by an AST architecture test
(`backend/tests/architecture/test_boundaries.py`).

## 2. Final architecture (as enforced, not as aspired)

- Modular monolith (ADR 0001): FastAPI + SQLAlchemy 2 (sync) + Pydantic v2.
  SQLite default; Postgres 16 via compose (both paths verified in containers).
- Composition root: `app/api/v1/*` (router auto-discovery). Leaf services:
  `policy`, `razorpay`, `revenue`, `detection`, `diagnosis`, `insights`.
  `agent` and `recovery` compose leaves under the encoded matrix.
  `evaluation` is a sanctioned second composition root (harness only, never
  on the serving path). `simulator` imports nothing from services.
- Execution adapter: `ports.PaymentGateway` — real raw-REST Razorpay client
  OR `SimulatedPaymentGateway` (labeled SIMULATION), selected by
  `SIMULATION_MODE`/keys. Mutations carry `gateway_request_id` idempotency
  keys (mapped to Razorpay `receipt`/`reference_id`); exactly-once is
  wire-asserted; timeout → UNKNOWN with GET-only resolution; opportunity row
  lock makes this hold under Postgres concurrency.
- Verification: HMAC-SHA256 over raw body (fail-closed), `x-razorpay-event-id`
  dedup (UNIQUE), out-of-order-safe state machine, link-paid amount/currency
  cross-check, service-layer handler registry reused by the reconcile sweep.
- Audit: every financial transition writes `audit_logs` (actor, request-id);
  property-style sweep test proves the chain.

Full detail: `docs/architecture.md`, `docs/data-flow.md`,
`docs/security-architecture.md` (all reviewed against code 2026-08-28).

## 3. Final metrics

### 3.1 Canonical evaluation (simulation; stored run `run_b371e5b40dc9450a88d052deb03809fe`)

Spec: scenario `standard`, seed 42, anchor 2026-08-28, holdout 0.10
(`ml/experiments/canonical_spec.json`; dataset `sim_42_2b55f523ad@2026-08-28`,
30 days, 68,410 payment events, 4,897 failed payment rows).

| metric | value | definition note |
|---|---:|---|
| Detection precision / recall / F1 | 0.333 / 0.667 / 0.444 | persisted incident rows vs injected ground truth, scheduled passes |
| Detection MTTD | 230 min | sim time |
| Diagnosis top-1 / top-3 | 1.000 / 1.000 (4 scored) | first-detection window vs ground truth, RF `v20260828T013109Z-77a4ef3b` |
| Interventions (PR vs baseline) | 100 vs 4,897 (98.0% fewer) | actions reaching the gateway |
| Recovered revenue, verified (PR) | 2,521,400 paise | gross; webhook-verified only |
| Baseline gross recovered | 95,016,400 paise | ungated, includes organic |
| False interventions (PR vs baseline) | 6 vs 421 | never-approve resubmissions |
| Unsafe actions | **0** | every run ever recorded |
| Holdout raw lift | −1.0 pp [−4.6, +2.1] | ITT, Newcombe 95% CI |
| Holdout class-adjusted lift | +0.1 pp [−2.9, +3.2] | post-stratified |
| Executed-action conversion vs organic | 28.0% vs ~14% | per-action read; fleet ITT is underpowered at ~2.3% coverage (documented) |

### 3.2 Multi-anchor robustness (`docs/evaluation.md` §3d, 7 anchors × 3 weeks)

Detection P 0.333–0.714, R 0.667–0.833, F1 0.444–0.769, MTTD 230–635 min;
diagnosis top-1 0.80–1.00; **unsafe actions 0 on every anchor**; every
holdout lift CI brackets zero. Anchor set = 4 distinct datasets × re-keyed
draws (disclosed). Reading: recall and safety are stable; precision is
window-sensitive — published as a robustness bound, not a blemish hidden.

### 3.3 Test evidence

| suite | count | status |
|---|---:|---|
| Backend tests | **678** | green (final run: this document's freeze run) |
| Playwright e2e | 7 | green |
| Payment invariants (`docs/payment-invariants.md`) | 12/12 | mechanically proven (30 dedicated tests) |
| Agent eval corpus (36 cases) | policy_compliance 1.0, unsafe 0.00 | zero gateway mutations |
| Security tests | 88 | green; 7 vulnerabilities found & fixed |

## 4. Reproducibility statement

- Same code + same seed + same anchor ⇒ same metrics: canonical spec run 3×
  → pairwise bit-identical metrics JSON except documented wall-clock fields
  (proof in `docs/evaluation.md` §3c; a 17th-significant-digit float wobble
  in one stored confidence is disclosed).
- Every run stores: scenario, seed, anchor/end_date, dataset version,
  diagnosis artifact id, policy version (content hash), harness git sha.
- Historical runs are preserved and cited by id in `docs/evaluation.md` §3b
  (version history); no historical record was altered during hardening.
- Clean-checkout reproduction: `git clone` → `docker compose build/up` →
  seeded demo + reset, verified 2026-08-28 on a pristine clone (§7).

## 5. Known limitations (complete list; nothing here is hidden)

1. **Simulation-bound**: all headline metrics are from the labeled simulator.
   The real Razorpay adapter is implemented and test-mode-ready but was
   exercised against Test Mode only for fail-safe verification (typed 401).
2. **Detection window sensitivity** (§3.2): precision varies by anchor;
   `route_latency`/organic multi-method swings can still admit (bounded).
3. **Diagnosis minority classes**: span macro-F1 0.766 (bank_downtime,
   subscription_failure_spike weak on exact spans; disclosed at exp07 ship;
   production frames improved: unsafe side 0.567→0.0928).
4. **Heuristic fallbacks**: without the model artifact, diagnosis is
   heuristic (conf ≤ 0.7) and all execution takes the approval lane.
5. **Demo-grade authN/Z**: single shared API key on mutations, open GETs,
   self-declared approver identity (production path documented).
6. **Synchronous long-running endpoints** (evaluation/demo triggers) —
   documented (ADR 0009); 120s client timeout.
7. **No worker tier**: delayed retries fire immediately; `notify_customer`
   records but does not send; reconciliation is operator-triggered (ADR 0011).
8. **Single merchant**; in-memory rate limiter and gateway singleton are
   single-process; SQLite single-writer ceiling in local mode.
9. **Baseline-arm measurement asymmetry**: baseline gross is conversion-draw
   while PulseRecover's is verified-only — the comparison is structurally
   conservative for PulseRecover (documented in `docs/evaluation.md`).
10. **Fleet ITT underpowered** at current policy coverage (~2.3%): the
    honest lift interval brackets zero; widening is a policy-file decision.

## 6. Security findings (all fixed, all regression-tested)

| finding | severity | fix |
|---|---|---|
| UNKNOWN-resolution identity confusion (false recovered revenue possible) | high | id-verified resolve + audit |
| Empty `API_KEY` failed open | high | fail-closed 503 |
| Webhook body exhaustion / deep-JSON 500 | medium | 1 MiB cap → 413; recursion → 400 |
| Advocacy sanitizer gaps in agent free-text | medium | all fields sanitized |
| NaN/Inf confidence crashed mutation tools pre-gate | medium | pre-validation; gate still blocks |
| **Postgres concurrent-execute double-fire (2 links, both RECOVERED)** | **critical** | opportunity row lock; 3/3 re-runs clean |
| `payment_link.paid` trusted reference_id only | medium | amount/currency cross-check; partials excluded |

Attack matrix: `docs/security-testing.md`. Invariants: `docs/payment-invariants.md`.
Accepted residual risks are listed there (sim-mode webhook secret,
ack detail verbosity, non-financial run-endpoint audit coverage).
Repo hygiene: no secrets (dummy `rzp_test_*` placeholders only), no scratch
files, no generated junk; `.gitignore`/`.dockerignore` verified.

## 7. Deployment verification

- Clean clone (`313890f` at the time) → `docker compose build` OK → stack up:
  Postgres healthy, alembic at head (22 tables), `/healthz` ok, system/health
  real checks (db ok, policy engine ok with content-hash version, gateway
  simulator), demo scenario seeded 41,354 rows, dashboard real numbers,
  diagnosis served by the committed RF artifact (not heuristic), unsigned
  webhook → 400, UI pages 200, mutations authorized via build-arg key,
  `/api/v1/demo/reset` → clean state. Zero reliance on untracked local state.
- Compose ports/API key parametrized (`BACKEND_PORT`/`FRONTEND_PORT`/
  `DB_PORT`/`API_KEY`); CORS JSON form; frontend standalone image.
- DB-outage behavior: `connect_timeout=3` → fast `database: down` (chaos F1).

## 8. Demo verification

- `docs/demo-script.md` (5-minute runbook) rehearsed twice on the deployed
  stack: totals 67.6s / 63.4s machine time (~4 min narration slack);
  **15/15 story beats verified with identical figures both passes**;
  first-60-seconds check PASS (`docs/demo-rehearsal.md`).
- Failure beats live-verified: gateway timeout → UNKNOWN (1 mutation ever)
  → GET-only resolve; unsafe AI refund → POLICY BLOCKED, 0 gateway calls.
- 9-beat chaos matrix (`docs/demo-chaos.md`): backend down, bad credentials,
  DB down, forged signature, out-of-order webhooks, gateway 500, stuck
  UNKNOWN, LLM garbage, missing artifact — every one fails visibly and
  safely with a defined recovery and talk track.

## 9. Release checklist (freeze gate — all true)

- [x] Canonical evaluation reproducible (§4)
- [x] No critical security findings open (§6)
- [x] No critical demo findings open (§8)
- [x] No stale claims (`docs/claim-matrix.md`, 116 claims audited)
- [x] Deployment clean from a pristine checkout (§7)
- [x] Frontend polished, all numbers trace to API (release-candidate pass)
- [x] Backend tests green — 678 (final run attested by the freeze commit)
- [x] E2E green — 7/7
- [x] Adversarial/agent suites green — 36/36, unsafe 0.00
- [x] Razorpay Test Mode path honest (real adapter + fail-safe proof; sim labeled)
- [x] Five-minute demo repeatable from reset (2 timed passes)

**FREEZE DECLARED.** Any post-freeze change requires re-running the full
checklist and bumping this document.

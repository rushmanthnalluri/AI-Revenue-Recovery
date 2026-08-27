# PulseRecover — Evaluation Methodology & Results

> ADR 0005: evaluation is scientific, not anecdotal. Every number in this
> document is reproduced by the commands at the bottom, against simulator
> ground truth. Nothing here is a vendor claim.

The harness (`app/services/evaluation/`) now answers two questions:

1. **Arm comparison** — does the full PulseRecover loop beat the industry
   default, "retry every failed payment once", and at what intervention cost?
2. **Causal measurement** — does the loop recover more than *doing nothing*?
   A randomized customer-level holdout inside the PulseRecover arm yields
   **incremental lift with confidence intervals** — the counterfactual-valid
   methodology no vendor in the market publishes
   (`docs/competitive-analysis.md` §5).

## 1. Experimental setup

Each run executes TWO arms of the same simulator scenario
(`app.simulator.SCENARIOS`, deterministic per seed):

- **BASELINE** — the naive default: for *every* failed payment, one generic
  retry (a fresh order via the gateway twin). No detection, no diagnosis, no
  policy gate, no verification. This is what "we retry failed payments"
  means at most merchants.
- **PULSECOVER** — the real product loop, unchanged: scheduled detection
  passes (`run_detection`) → ML diagnosis (`DiagnosisService`) → opportunity
  + strategy generation → the deterministic policy gate (`RecoveryExecutor`,
  `policies/default.yaml`) → execution through the `PaymentGateway` port →
  verification through the real webhook handler registry and
  `RecoveryExecutor.resolve`. **Inside this arm, a deterministic
  customer-level holdout (default 10%) receives no recovery actions** — §2.

Arms run in **isolated scratch SQLite databases** (tempfiles, deleted
afterwards). The main/demo database receives only the resulting
`evaluation_runs` + `experiments` rows. The scratch `simulator_run_id`s are
recorded inside the metrics JSON (`arms.*.simulator_run_id`);
`evaluation_runs.simulator_run_id` stays NULL rather than pointing at a row
that does not exist in the main database. The `experiments` row carries the
full pre-registered configuration (scenario, conversion model, holdout
fraction, estimand, CI method) and the lift summary in its results.

### Roles the harness plays (all deterministic, all disclosed)

- **The operator**: when the policy gate returns `REQUIRES_APPROVAL`, the
  harness approves as `human:eval_operator` and re-executes. This is the
  human-in-the-loop the product is designed for; `approvals_required` counts
  how often it happened.
- **The customer (under action)**: decides whether a recovery attempt
  converts, via a documented (failure-class × action) conversion table
  (`CONVERSION` in `runner.py`). One table governs both arms — the baseline
  always lands in the `immediate_retry` column. Payment links are decided
  *inside* the gateway twin (flat `GATEWAY_SUCCESS_RATE` — a disclosed twin
  limitation: the twin's inline link decision is class-independent).
- **The customer (under no action)**: decides whether a failure the loop
  never touched *self-resolves*, via the `no_action` column of the same
  table (§2). This third role is what makes the holdout a real control
  instead of a zero by construction.
- Detection passes are scheduled, not ground-truth-anchored: one pass every
  6h of simulated time, each looking back 12h
  (`DETECTION_STEP_MINUTES`/`DETECTION_WINDOW_MINUTES`). The 12h window is
  what gives the zscore detector enough pre-anomaly baseline to fire on
  1.5–3h injected incidents; the detector and thresholds are the production
  defaults. Detection therefore scores *honestly*: passes also cover quiet
  data, and false positives are counted.

### Reproducibility (same seed → identical metrics)

- Draws are seeded on **stable simulator identities** (payment/order id),
  never on per-run random ids, and app-id allocation inside each arm runs
  under a deterministic-id guard (`EvaluationRunner._deterministic_ids`) —
  request ids feed the twin's payment-link outcomes and the harness's
  conversion draws, so reproducible ids are what make outcomes reproducible.
- Holdout membership is a pure function of (run seed, customer id) — §2.
- Verified at standard scale: two consecutive `--scenario standard --seed 42`
  runs produce **bit-identical metrics** (checked over the full metrics
  JSON). The single exception is wall-clock MTTR — an operational pipeline
  measurement, not a simulator output.

### Metrics and their exact definitions

- **Detection precision/recall/F1** vs `simulator_ground_truth`: a detected
  incident matches a ground-truth incident when its flagged span
  (`meta.anomaly_start..anomaly_end`, else its analysis window) overlaps the
  injected window. Detection now persists one episode row per
  (metric, detector, segment) episode after noise floors + episode merge —
  see `docs/detection.md` for the redesign and its measured effect.
- **MTTD (minutes, simulator time)**: first pass window-end overlapping the
  injected window minus the injected start.
- **Diagnosis top-1/top-3**: each ground-truth incident is scored once, via
  the diagnosis of its *first-detected* matching incident, against the
  taxonomy mapping (`KIND_TO_CAUSE`; a method outage with a targeted bank is
  `bank_downtime`).
- **Recovered revenue / recovery rate**: webhook- or inline-verified
  `RECOVERED` actions only. UNKNOWN actions are never counted. The
  denominator is **all first-attempt failed payment rows, snapshotted before
  any recovery action runs** — previously it was queried after recovery,
  which silently dropped verified recoveries from the denominator (their
  payments flip to `captured`); fixed and disclosed here. The simulator's
  own customer checkout-retries are separate, unmarked payment rows, so the
  denominator is "all failed payment rows", symmetric across arms and
  groups.
- **Interventions / false interventions**: an intervention is an action that
  reached the gateway. A *false* intervention targets a never-approve
  (`hard_decline`) payment — network guidance says do not resubmit those.
- **Unsafe actions**: executions with neither an `ALLOWED` policy decision
  nor a recorded human approval, plus any `refund` execution. **Asserted 0
  in the test suite.**
- **MTTR (minutes, wall clock)**: `proposed_at → verified_at` pipeline
  latency. Sim-time MTTR is meaningless when execution is synchronous, so
  the honest operational number is reported and labeled.

### Policy rate-limit note (simulation artifact)

Policy rate windows (`max_actions_global_per_hour`, per-customer-day) are
evaluated in wall-clock time while the data is simulator time: an evaluation
run executes all its actions "in the same hour". At the current detection
precision the binding constraint is `max_actions_per_incident: 10`
(6 detected incidents → 60 executed actions); the global cap no longer
binds. This is the deterministic gate working as configured, not a harness
bug; it is visible in `policy_outcomes.BLOCKED`, and — as §3 shows — it
defines how much the fleet-level lift measurement can resolve.

## 2. The randomized holdout (pre-registered)

Design pre-registered in `docs/product-strategy.md` §4.1/§7 and implemented
exactly as specified there.

- **Assignment.** Within the PulseRecover arm, each *customer* is assigned
  by `sha256("holdout:{seed}:{customer_id}")` (first 8 bytes / 2⁶⁴) —
  holdout when the value is below the configured fraction (default **0.10**,
  pre-registered band 5–10%). Customer-level, platform-independent, and
  identical for any two runs with the same seed. Payments without a customer
  id stay in treatment (disclosed rule). Detection and diagnosis still run
  fleet-wide for held-out customers; **only the recovery loop is withheld**:
  opportunities are never built for them (`HoldoutExcludingBuilder`), so no
  strategies, actions, or gateway calls ever reach them. The harness counts
  holdout opportunities/actions on every run and the test suite asserts
  both are 0.
- **Estimand.** `incremental lift = recovery_rate(treatment) −
  recovery_rate(holdout)` over the run's fixed attribution window. The
  denominator is **ALL first-attempt failed payments in scope for each
  group** — no eligibility cherry-picking (intent-to-treat at fleet level:
  most failures are organic noise the loop never touches, and they stay in
  the denominator).
- **Attribution window.** Each payment's failure timestamp → the
  scenario-end anchor (latest terminal simulator event), fixed per run.
- **The no-action counterfactual.** Both groups share the same organic
  baseline: a failure the loop never executed an action against self-
  resolves with the documented `no_action` prior for its failure class
  (timeout 0.30, soft_decline 0.20, insufficient_funds 0.08, abandonment
  0.06, hard_decline 0.01, unknown 0.06 — priors in the same disclosed
  family as the action columns, set far below e.g. Recurly's observed ~67%
  soft-decline self-resolution). Self-resolution lag is uniform (0, 7 days],
  right-censored at the window end (censored = counted not recovered).
  Payments with an executed action resolve on the action's own draw only —
  no double-counting. **Every self-resolution, in both groups, is captured
  through the real signed-webhook path** — the strict gateway/webhook-
  verified standard is identical on both sides; a payment without a gateway
  id can never be counted.
- **Statistics.** Primary: two-proportion difference with a **Newcombe
  hybrid score (Wilson) 95% CI** (Newcombe 1998, method 10) — closed form,
  deterministic, tiny-count safe: small groups yield honestly wide bands,
  never a bare point estimate. Secondary: **class-standardized lift** —
  pooled-weight post-stratification over the pre-registered failure-class
  strata (removes chance class-mix imbalance between the randomized
  groups), normal-approximation CI. Stratification is reported by failure
  class and by method, each stratum with its own Newcombe CI.
- **Time-to-recovery.** Median per group. Treatment action recoveries land
  at the scenario end (batch-execution artifact — the harness executes all
  actions after the last detection pass; production latency is wall-clock
  MTTR); organic recoveries use their sampled lag. Window-end censoring
  applies to organic resolutions only.

## 3. Results (reproduced, this machine)

<!-- RESULTS-START -->

Run `final` / `run_caa1f1a90ef243d08860679b6631fe6d` — scenario `standard`
(full preset: 30 days, 68,993 payment_events, 4,918 failed payment rows,
6 injected incidents), seed 42, holdout fraction 0.10, diagnosis artifact
`logistic_regression v20260826T234303Z-c5434878` (see docs/ml.md §8).
Wall-clock: ~3.5 min.

**Detection (scheduled 12h/6h passes, production defaults, post-redesign):**
precision **0.667**, recall **0.500** (3/6 injected incidents found),
F1 0.571, MTTD **895 min** over 116 passes and 6 persisted incident rows
(4 matched). The three misses are coverage gaps, not floor casualties —
`route_latency` (a single route barely moves merchant-wide p90),
`checkout_abandonment_spike` (abandoned checkouts never become terminal
outcomes), `customer_insufficient_funds_wave` (night buckets below the
volume floor) — see docs/detection.md. Before the detection redesign this
same dataset scored precision 0.156 on 90 incident rows.

**Diagnosis (first-detection window per incident, vs ground truth):**
top-1 **0.667**, top-3 **0.667** (3 scored). The gateway degradation and
the method outage are top-1 correct at confidence 0.72/0.92; the 48h
subscription spike reads as `no_fault` from the diluted 12h window. On
exact incident spans the same model is 6/6 (docs/ml.md §8) — the gap is
window dilution, and it argues for re-scoping the incident window to the
detected anomaly span before diagnosis.

**Recovery (arm comparison):**

| metric | BASELINE (retry everything) | PULSECOVER |
|---|---:|---:|
| interventions | 4,918 | **60** (98.8% fewer) |
| recovered revenue (verified, action-attributed) | 99,025,100 paise | 1,380,700 paise |
| recovery rate (of failed amount) | 27.2% | 0.38% |
| false interventions (never-approve resubmissions) | **432** | **5** |
| unsafe actions (no gate, no approval) | 4,918 ungated | **0** |
| UNKNOWN / unverifiable outcomes | n/a (no verification) | 0 |
| human approvals required | 0 | 58 |

655 opportunities were built inside the 6 detected incident windows; the
gate blocked 595 proposals (per-incident cap, duplicate protection) and sent
58 of the 60 executed actions through the human-approval lane (2 ALLOWED
outright); 16 actions verified RECOVERED (26.7% of executed).

**Honesty correction vs the previous version of this document.** The stale
table showed 1,945,400 paise over 23 recovered actions and 100
interventions. The current number is lower for three disclosed reasons, all
of which make it *more* honest: (1) the detection precision redesign
(docs/detection.md) removed 84 organic-noise incident rows whose windows
had been generating "recovery" work against ordinary failures — gross-
attribution inflation of exactly the kind this document criticizes;
(2) the holdout now withholds actions from ~9% of customers by design;
(3) recovery draws were re-keyed onto stable simulator ids for bit-exact
reproducibility. The baseline's 27.2% gross rate is unchanged in kind —
and under the harness's own priors it decomposes as ≈14–15pp organic
self-resolution plus ≈12pp retry-attributable effect, which is precisely
the over-statement the holdout exists to measure.

**Incremental lift (randomized holdout, this run):**

| group | failed payments | recovered | recovery rate | median TTR |
|---|---:|---:|---:|---:|
| treatment (full loop) | 4,445 | 639 (16 via actions + 623 organic) | 14.38% | 4,630 min |
| holdout (no action) | 473 | 86 (all organic) | 18.18% | 4,854 min |

- **Raw ITT lift (pre-registered primary): −3.8 pp, 95% CI [−7.7, −0.4]**
  (Newcombe/Wilson).
- **Class-standardized lift (secondary): −2.7 pp, 95% CI [−6.1, +0.7]** —
  brackets zero once the chance class-mix imbalance is removed (the
  randomized holdout landed timeout-heavy: 23.3% of its failures vs 19.5%
  in treatment, and timeout carries the highest organic prior).
- Isolation: **0** opportunities and **0** actions for held-out customers
  (asserted). Unsafe actions: **0**. Assignment: 1,700 treatment / 165
  holdout customers (realized 8.85% at configured 10%). Attribution window:
  up to 698.1 h (~29 days).

Per failure class (treatment rec/n, holdout rec/n, lift [95% CI]):

| class | treatment | holdout | lift [CI] |
|---|---|---|---|
| soft_decline | 300/1,627 (18.4%) | 42/186 (22.6%) | −4.1 pp [−10.9, +1.6] |
| insufficient_funds | 67/1,014 (6.6%) | 6/81 (7.4%) | −0.8 pp [−8.7, +3.5] |
| timeout | 235/867 (27.1%) | 31/110 (28.2%) | −1.1 pp [−10.5, +7.1] |
| abandonment | 32/550 (5.8%) | 6/51 (11.8%) | −5.9 pp [−17.7, +0.7] |
| hard_decline | 5/387 (1.3%) | 1/45 (2.2%) | −0.9 pp [−10.3, +1.6] |

Per method: upi −0.4 pp [−6.1, +4.3]; card −5.5 pp [−12.7, +0.0];
netbanking −9.9 pp [−20.3, −1.5]; wallet −4.7 pp [−23.3, +5.9].

**How to read this — the measurement is the product.** Three facts
decompose the headline:

1. **The actions that execute do work.** Verified conversion on executed
   actions is 26.7% (16/60) against a ≈14.4% fleet-wide organic baseline
   (709/4,918) — consistent with the disclosed priors, in which every
   action column sits above `no_action` for its class.
2. **The fleet-level effect is structurally tiny at the current policy
   envelope.** The gate (`max_actions_per_incident: 10`, 6 detected
   incidents) executes 60 actions over 4,445 treatment failures — 1.3%
   coverage. The expected ITT effect is ≈ +0.2–0.4 pp, while the
   between-group sampling error of the organic baseline at these sample
   sizes is ±1.9 pp (MDE ≈ 5 pp). The experiment is **underpowered for the
   effect the current configuration can produce** — the exact phenomenon
   Lewis & Rao 2015 describe for lift measurement (cited in
   docs/competitive-analysis.md §5).
3. **The negative raw point is counterfactual luck, not harm.** Treatment-
   side organic realized almost exactly its model expectation (14.0% vs
   ≈14.0%); the holdout side drew hot (18.2% vs ≈15–16% expected) on top of
   the timeout-heavy class mix. The class-standardized estimator removes
   the mix component and its CI brackets zero; per-stratum CIs are wide by
   design.

A vendor headline would have reported "26.7% verified conversion on
executed actions" (or the baseline's 27.2% gross) and stopped there. The
holdout exists to show what such numbers hide — and to make the uncertainty
explicit rather than invisible. Tightening the ITT estimate is a policy-
file decision (wider per-incident caps) or a longer-horizon run, not a code
change; both are follow-ups, disclosed here rather than silently tuned.

<!-- RESULTS-END -->

## 4. Honest limitations

- The customer conversion table — including `no_action` — is a documented
  prior model, not a measured fact. Both arms and both holdout groups share
  it, so every *comparison* is fair even where absolute numbers are
  prior-driven. The holdout's organic baseline is the same kind of prior
  as the action columns, layered on top of the simulator's intrinsic
  customer retries (separate payment rows, symmetric across groups).
- At the demo policy envelope the fleet ITT lift is underpowered (§3): the
  holdout methodology is demonstrated end-to-end, but the point estimate at
  this scale is dominated by organic-baseline sampling variation. Do not
  quote the point without its CI.
- Sim-time treatment time-to-recovery is a batch artifact: the harness
  executes all actions at the scenario end. The operational latency measure
  is wall-clock MTTR.
- Detection still misses three incident kinds (§3; docs/detection.md) and
  its precision is 0.667, not 1.0 — both stated, both measured.
- SQLite-scale synchronous execution: `POST /api/v1/evaluation/run` blocks
  for the whole run (seconds at reduced scale; minutes at full preset
  scale). That is deliberate for the demo — the CLI is the full-scale path.

## 5. Reproduction

```bash
cd backend
# full-preset run with the default 10% holdout (minutes; persists the run row)
.venv/Scripts/python scripts/run_evaluation.py --scenario standard --seed 42
# explicit holdout fraction / disabled holdout
.venv/Scripts/python scripts/run_evaluation.py --scenario standard --seed 42 --holdout-fraction 0.10
.venv/Scripts/python scripts/run_evaluation.py --scenario standard --seed 42 --holdout-fraction 0
# faster smoke run at reduced scale
.venv/Scripts/python scripts/run_evaluation.py --scenario upi_outage_demo --days 5 --events 8000
# same, via the API (synchronous)
curl -X POST localhost:8000/api/v1/evaluation/run -H 'X-API-Key: dev-key' \
  -H 'Content-Type: application/json' \
  -d '{"scenario": "upi_outage_demo", "holdout_fraction": 0.10}'
# stored rows only:
curl localhost:8000/api/v1/evaluation/runs
curl localhost:8000/api/v1/evaluation/metrics   # includes incremental_lift
```

Safety invariants enforced by the test suite
(`tests/evaluation/`, `tests/integration/test_evaluation_api.py`): zero
unsafe actions; zero opportunities and zero actions for held-out customers;
bit-identical metrics across identical-seed runs (wall-clock MTTR
excepted); realized holdout membership within tolerance of the configured
fraction; CI always brackets the point estimate, including at tiny counts.

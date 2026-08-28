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

### Reproducibility (same seed + same day → identical metrics)

- Draws are seeded on **stable simulator identities** (payment/order id),
  never on per-run random ids, and app-id allocation inside each arm runs
  under a deterministic-id guard (`EvaluationRunner._deterministic_ids`) —
  request ids feed the twin's payment-link outcomes and the harness's
  conversion draws, so reproducible ids are what make outcomes reproducible.
- Holdout membership is a pure function of (run seed, customer id) — §2.
- **The time anchor is calendar-day dependent.** With `end_date` unset
  (the scenario presets), the simulator anchors the 30-day window to
  *today* 00:00 UTC (`app/simulator/engine.py`). The config hash — and
  therefore `simulator_run_id` — does not include it, so two runs on
  different days share the run id and configuration but see a one-day-
  shifted dataset. Same seed + same day → bit-identical; same seed +
  different day → same experiment, shifted data. Cross-day comparisons in
  this document always state both anchors (§3/§3b).
- Verified at standard scale on 2026-08-28: two `--scenario standard
  --seed 42` runs (`canonical-v2` / `canonical-v2-repro`,
  `run_b371e5b4…` / `run_5d22f898…`) produce **bit-identical metrics**
  over the full metrics JSON, with two disclosed exceptions: wall-clock
  MTTR (an operational pipeline measurement, not a simulator output) and
  a 17th-significant-digit float-serialization difference in one stored
  diagnosis confidence (0.4796502975443439 vs 0.47965029754434385) — no
  draw, count, rate, or CI differs.

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
run executes all its actions "in the same hour". On the canonical run both
brakes bind: the per-incident cap (`max_actions_per_incident: 10`) and —
with the stuck-checkout source pushing proposal volume past it — the
global cap (`max_actions_global_per_hour: 100`), which the 100 executed
actions hit exactly; every later proposal, including all 356 stuck-checkout
ones, is BLOCKED (§3). This is the deterministic gate working as configured,
not a harness bug; it is visible in `policy_outcomes.BLOCKED`, and — as §3
shows — it defines how much the fleet-level lift measurement can resolve.

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

## 3. Canonical results (reproduced, this machine)

<!-- RESULTS-START -->

Run `canonical-v2` / `run_b371e5b40dc9450a88d052deb03809fe` — scenario
`standard` (full preset: 30 days of traffic anchored to **2026-08-28
00:00 UTC**, 68,410 simulator payment_events, 4,897 first-attempt failed
payment rows totalling 364,686,600 paise, 6 injected incidents), seed 42,
holdout fraction 0.10, diagnosis artifact
`random_forest v20260828T013109Z-77a4ef3b` (the exp07 SHIP decision,
docs/ml.md §10). Wall-clock: ~1.5 min. Stored and browsable in the
Evaluation Lab. A same-day repeat (`canonical-v2-repro` /
`run_5d22f898ef7d45189ec5e990e0169ed5`) reproduces every metric
bit-for-bit, with the two disclosed exceptions of §1 (wall-clock MTTR;
a 17th-significant-digit float in one stored diagnosis confidence).

> **Reading cross-run comparisons:** the simulator anchors an unset
> `end_date` to *today* 00:00 UTC (`app/simulator/engine.py`) — the same
> seed generates the same dataset within a calendar day and a one-day-
> shifted 30-day window across days (the config hash, and therefore
> `simulator_run_id` `sim_42_50f24b57d0`, is unchanged). The §3b history
> runs were generated on 2026-08-27, the canonical run on 2026-08-28:
> same scenario, same seed, but the window slid by one day, so the
> datasets differ at the margins (4,918 → 4,897 failed rows; baseline
> gross 99,025,100 → 95,016,400 paise). Every cross-day delta below is
> attributed either to the shifted window or to a shipped change called
> out by name — never silently to "the code".

**Detection (scheduled 12h/6h passes, production defaults, detection v2):**
precision **0.333**, recall **0.667** (4/6 injected incidents found),
F1 0.444, MTTD **230 min** over 116 passes and 12 persisted incident rows
(4 matched / 8 unmatched). Found: `gateway_degradation` (surfaced by the
per-route latency scan — the injected degradation carries a latency
signature), `method_outage`, `route_latency`,
`checkout_abandonment_spike`. Missed on this window:
`subscription_failure_spike` and `customer_insufficient_funds_wave` (no
`insufficient_fund_share` episode crossed the floors). Of the 8 unmatched
rows, 4 are latency-scan episodes and 4 success-rate episodes firing on
organic noise. This is the same engine that scored 6/6 at precision 0.778
on the 2026-08-27 anchor (§3b): the recall/precision swing is **window
sensitivity, not a code change** — no detection or simulator code changed
between the runs. MTTD improved 585 → 230 min.

**Diagnosis (first-detection window per matched incident, vs ground
truth):** top-1 **1.000**, top-3 **1.000** (4 scored) — the exp07
random-forest artifact labels all four matched windows correctly
(`gateway_degradation` 0.48, `method_outage` 0.98, `route_latency` 0.60,
`abandonment_spike` 0.76). Denominator honesty: the two incidents the
previous logistic-regression artifact misread as `no_fault` on 2026-08-27
are exactly the two this run does not detect, so they drop out of the
scored set — on the four windows both runs share, the old artifact was
also 4/4. The case for the RF swap rests on the pre-registered exp06
gate (span top-1 0.910 vs 0.878; unsafe-side error 0.567 → 0.093), not
on this table — docs/ml.md §10.

**Recovery (arm comparison):**

| metric | BASELINE (retry everything) | PULSECOVER |
|---|---:|---:|
| interventions | 4,897 | **100** (98.0% fewer) |
| recovered revenue (verified, action-attributed) | 95,016,400 paise | 2,521,400 paise |
| recovery rate (of failed amount) | 26.1% | 0.69% |
| false interventions (never-approve resubmissions) | **421** | **6** |
| unsafe actions (no gate, no approval) | 4,897 ungated | **0** |
| UNKNOWN / unverifiable outcomes | n/a (no verification) | 0 |
| human approvals required | 0 | 94 |

1,472 opportunities were built inside the 12 detected incident windows —
1,116 `failed_payment_retry` plus **356 `stuck_checkout_payment`**, the
source shipped after the §3b runs (payments stranded in `created` > 30
min; ₹254,080 of abandoned checkouts surfaced). The gate blocked 1,372
proposals and sent 94 of the 100 executed actions through the human-
approval lane (6 ALLOWED outright). Executed: 94 `retry_payment` + 6
`create_payment_link`; 28 verified RECOVERED — 28.0% of executed (25
retries + 3 links). **All 356 stuck-checkout proposals were BLOCKED**:
the per-incident cap (`max_actions_per_incident: 10`) appears in 203 of
the 356 decisions and the global hourly brake
(`max_actions_global_per_hour: 100`) in 181 — the 100 executed actions
hit it exactly (§1's wall-clock note) — 9 also hit the
`customer_opted_out` hard block and 2 the duplicate cooldown. The
deterministic gate absorbing a volume spike exactly as configured; the
stuck-checkout lane is proven end-to-end in
`tests/recovery/test_stuck_checkout.py`, but at the demo policy envelope
it surfaces recoverable value without yet executing against it.

**Incremental lift (randomized holdout, this run):**

| group | failed payments | recovered | recovery rate | median TTR |
|---|---:|---:|---:|---:|
| treatment (full loop) | 4,424 | 610 (28 via actions + 582 organic) | 13.79% | 4,874 min |
| holdout (no action) | 473 | 70 (all organic) | 14.80% | 5,157 min |

- **Raw ITT lift (pre-registered primary): −1.0 pp, 95% CI [−4.6, +2.1]**
  (Newcombe/Wilson) — brackets zero.
- **Class-standardized lift (secondary): +0.1 pp, 95% CI [−2.9, +3.2]** —
  centered on zero once the chance class-mix imbalance is removed (the
  randomized holdout again landed timeout-heavy: 23.5% of its failures vs
  19.8% in treatment, and timeout carries the highest organic prior).
- Isolation: **0** opportunities and **0** actions for held-out customers
  (asserted — with the stuck-checkout source now in the builder, its
  holdout exclusion is covered by the shipped fix and its regression
  test). Unsafe actions: **0**. Assignment: 1,677 treatment / 170 holdout
  customers (realized 9.2% at configured 10%). Attribution window: up to
  698.1 h (~29 days).

Per failure class (treatment rec/n, holdout rec/n, lift [95% CI]):

| class | treatment | holdout | lift [CI] |
|---|---|---|---|
| soft_decline | 287/1,647 (17.4%) | 31/189 (16.4%) | +1.0 pp [−5.2, +6.0] |
| insufficient_funds | 76/984 (7.7%) | 3/79 (3.8%) | +3.9 pp [−3.0, +7.0] |
| timeout | 214/874 (24.5%) | 32/111 (28.8%) | −4.3 pp [−13.8, +3.8] |
| abandonment | 27/545 (5.0%) | 3/47 (6.4%) | −1.4 pp [−12.3, +3.3] |
| hard_decline | 6/374 (1.6%) | 1/47 (2.1%) | −0.5 pp [−9.6, +2.0] |

Per method: upi −4.2 pp [−10.0, +0.7]; card +5.2 pp [−0.6, +8.9];
netbanking −1.8 pp [−11.2, +4.9]; wallet −4.5 pp [−25.5, +4.3].

**How to read this — the measurement is the product.** Three facts
decompose the headline:

1. **The actions that execute do work.** Verified conversion on executed
   actions is 28.0% (28/100) against a ≈13.3% fleet-wide organic baseline
   (652/4,897) — consistent with the disclosed priors, in which every
   action column sits above `no_action` for its class.
2. **The fleet-level effect is structurally tiny at the current policy
   envelope.** The gate executed 100 actions over 4,424 treatment
   failures — 2.3% coverage. The expected ITT effect is ≈ +0.3 pp, while
   the between-group sampling error of the organic baseline at these
   sample sizes is ±1.7 pp (MDE ≈ 5 pp). The experiment is **underpowered
   for the effect the current configuration can produce** — the exact
   phenomenon Lewis & Rao 2015 describe for lift measurement (cited in
   docs/competitive-analysis.md §5).
3. **The point estimate is noise, in either direction.** On the
   2026-08-27 anchor the holdout drew hot (18.2% organic) and the raw CI
   excluded zero below; on this anchor both groups realized within ~1 pp
   of each other and the class-adjusted estimate sits at +0.1 pp. Both
   readings live inside the same ±5 pp measurement band — which is the
   point: at this scale the ITT estimate cannot resolve the effect the
   current configuration produces, and the CI is the honest statement of
   that.

A vendor headline would have reported "28.0% verified conversion on
executed actions" (or the baseline's 26.1% gross) and stopped there. The
holdout exists to show what such numbers hide — and to make the uncertainty
explicit rather than invisible. Tightening the ITT estimate is a policy-
file decision (wider per-incident caps) or a longer-horizon run, not a code
change; both are follow-ups, disclosed here rather than silently tuned.

### 3b. Version history (stored runs, same scenario/seed)

All rows: scenario `standard`, seed 42, holdout 0.10, stored and browsable
in the Evaluation Lab. Dates are the dataset anchors (§3's cross-run note).

| run | anchor | code state | detection P / R / F1 | MTTD | diagnosis top-1 (scored) | opportunities / interventions | recovered (verified) | raw ITT lift [95% CI] |
|---|---|---|---|---|---|---|---|---|
| `run_caa1f1a90ef243d08860679b6631fe6d` ("final") | 2026-08-27 | detection v1 + first holdout arm | 0.667 / 0.500 / 0.571 | 895 min | 0.667 (3) | 655 / 60 | 1,380,700 paise | −3.8 pp [−7.7, −0.4] |
| `run_0022000d8df942e6ac4b7299986f994a` ("detection-recall:after-new-signals") | 2026-08-27 | detection v2 (three recall signals) | 0.778 / 1.000 / 0.875 | 585 min | 0.667 (6) | 903 / 90 | 2,452,900 paise | −3.7 pp [−7.6, −0.4] |
| `run_b371e5b40dc9450a88d052deb03809fe` ("canonical-v2", §3) | 2026-08-28 | + stuck-checkout loop, exp07 RF artifact, link-paid cross-check, stuck-source holdout fix | 0.333 / 0.667 / 0.444 | 230 min | 1.000 (4) | 1,472 / 100 | 2,521,400 paise | −1.0 pp [−4.6, +2.1] |

What changed between rows, and why:

- **v1 → v2 (same 2026-08-27 dataset, same day):** the detection redesign
  shipped three evidence-gated signals (per-route latency scan,
  `checkout_abandonment_rate`, `insufficient_fund_share`), closing all
  three then-blind spots — recall 3/6 → 6/6, precision 0.667 → 0.778,
  MTTD 895 → 585 min, zero new quiet-control false positives (before/after
  runs `run_4f3b346e88e74d3d91c4fba2c2caa94a` / `run_0022000d…`; design
  and rejected iterations in docs/detection.md and
  `ml/experiments/detection/exp000–003`). Newly surfaced recoverable
  revenue at risk on that anchor: ₹173,659. Recovered revenue rose
  1,380,700 → 2,452,900 paise (+77.7%) on the wider honest detection net.
- **v2 → canonical (one-day window shift + four shipped changes):**
  opportunities 903 → 1,472 and interventions 90 → 100 came from the
  stuck-checkout opportunity loop (`786ee19`), whose held-out-customer
  exclusion was fixed in `37bc124` (isolation 0/0 above, with 356 stuck
  proposals in play); the `payment_link.paid` amount/currency cross-check
  (`e894873`) made link verification stricter with no recovered-revenue
  casualty (3 links still verify); the exp07 RF diagnosis artifact
  (`799dfc7`) replaced the LR pointer (docs/ml.md §10). Detection's
  recall/precision swing (1.000/0.778 → 0.667/0.333) is attributable to
  the shifted window — the detection and simulator code is byte-identical
  across the two runs — and is disclosed as window sensitivity in §4.
  The holdout's hot draw on 2026-08-27 (18.2% organic) did not repeat
  (14.8%), so the raw ITT CI moved from excluding zero below to
  bracketing it; both are inside the run's measurement band.

(History kept for provenance: before the precision redesign, this dataset
family scored precision 0.156 on 90 incident rows. The first holdout run
also corrected a stale 1,945,400-paise table down to 1,380,700 — noise-
window "recovery" no action caused was removed, and the holdout began
withholding ~9% of customers by design. That correction is why gross
numbers in this document are always read alongside the holdout.)

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
- **Detection is window-sensitive.** The same v2 engine scored recall 6/6
  at precision 0.778 on the 2026-08-27 anchor and recall 4/6 at precision
  0.333 on the 2026-08-28 anchor (§3/§3b) — misses and false-positive
  volume move with the traffic the window happens to contain. Both
  readings are stated and measured; multi-anchor replay is the follow-up.
- **The dataset is calendar-day anchored** (§1): "same seed" reproduces
  bit-for-bit only within a calendar day; cross-day runs share the
  configuration, not the data. Pinning `end_date` for cross-day
  bit-reproducibility is a disclosed follow-up, not done here.
- SQLite-scale synchronous execution: `POST /api/v1/evaluation/run` blocks
  for the whole run (seconds at reduced scale; minutes at full preset
  scale). That is deliberate for the demo — the CLI is the full-scale path.

## 5. Reproduction

```bash
cd backend
# full-preset run with the default 10% holdout (minutes; persists the run row)
.venv/Scripts/python scripts/run_evaluation.py --scenario standard --seed 42
# same, with a readable run name (how §3's canonical-v2 was produced)
.venv/Scripts/python scripts/run_evaluation.py --scenario standard --seed 42 --name canonical-v2
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

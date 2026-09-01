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
  this document always state both anchors (§3/§3b). `run_evaluation.py
  --end-date` pins the anchor explicitly; the canonical spec (§3c) uses it
  to make the canonical evaluation reproducible on any day.
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
also 4/4. The case for the RF swap rests on the pre-registered campaign
gate (exp06 clauses, applied in exp07: exact-span top-1 0.910 vs 0.878;
prod-frame unsafe-side error 0.567 → 0.093), not
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

### 3c. Canonical evaluation specification (pinned, cross-day reproducible)

The canonical evaluation is now an explicit, machine-checkable contract:
`ml/experiments/canonical_spec.json`. It pins every input the metrics
depend on — scenario `standard`, seed 42, **`end_date` 2026-08-28** (the
anchor `run_b371e5b4` resolved to on its run day, so the spec reproduces
that run's dataset window), holdout 0.10, diagnosis artifact
`random_forest v20260828T013109Z-77a4ef3b`, policy
`1.0+sha256.5a6afe61d6db`, harness git sha — and records the exact
command:

```bash
cd backend && .venv/Scripts/python scripts/run_evaluation.py \
  --scenario standard --seed 42 --end-date 2026-08-28 --name canonical-v2
```

**Run-record completeness.** Every run now stores `metrics.dataset`
(scenario, seed, `simulator_run_id`, pinned `end_date`, resolved `anchor`,
and `dataset_version` = `<simulator_run_id>@<anchor-date>`) and
`metrics.versions` (`diagnosis_artifact`, `policy` — the content-hashed
policy file the gate evaluated with); the same blocks land in the
`experiments` row's pre-registered config. Additive: older runs simply
lack these keys.

**Verified 2026-08-28 — three consecutive spec runs**
(`run_4495b4460b314c16988b532828c304f2`,
`run_f02fd0489191419ab89c0909f34abd06`,
`run_33b4a0304d5f448b851bf92662f3ac18`, each against its own scratch
database): stored metrics are **pairwise bit-identical** with only the two
documented exception classes of §1 — wall-clock MTTR (which in fact
coincided at 0.0005 min in all three) and the 17th-significant-digit float
wobble in stored diagnosis confidences (0.4796502975443439 vs
…4385; 0.7639152548558948 vs …8947 across pairs). No draw, count, rate,
revenue figure, or CI differs. **Anchor flow is proven** by the stored
records: the same harness at tiny scale with `--end-date 2026-08-20`
records anchor `2026-08-20T00:00:00+00:00` /
`sim_11_75ecab6ca2@2026-08-20`; with `--end-date 2026-08-21`,
`2026-08-21T00:00:00+00:00` / `sim_11_b4437152cf@2026-08-21`.

**How the pinned spec relates to `run_b371e5b4`.** Same seed + same
2026-08-28 anchor ⇒ identical dataset content: detection 0.333/0.667/0.444,
MTTD 230 min over 116 passes, diagnosis top-1/top-3 1.000 (4 scored),
4,897 failed rows / 364,686,600 paise, 100 interventions / 94 approvals,
421 baseline false interventions, 0 unsafe, 0/0 isolation — all match the
§3 run exactly. But pinning `end_date` enters it into the config hash, so
the pinned run's `simulator_run_id` is `sim_42_2b55f523ad` (vs the
historical `sim_42_50f24b57d0`). Entity ids embed the run id, so the two
id-keyed randomizations re-key: holdout assignment (sha256 over the
customer id — realized 10.1% here vs 9.2% historically) and the
per-payment conversion draws. Draw-derived numbers therefore legitimately
differ from §3: opportunities 1,457 (vs 1,472), verified recovered
3,670,700 paise (vs 2,521,400), raw ITT lift **+2.2 pp [−0.9, +5.0]**
(vs −1.0 pp [−4.6, +2.1]) — same underpowered band and the same §3
conclusion: the point estimate is noise in either direction at this policy
envelope. The spec's guarantee is run-to-run bit-reproducibility of the
pinned evaluation on any day, not retroactive bit-identity with the
historical unpinned run.

### 3d. Multi-anchor robustness: the canonical spec across 7 calendar anchors

The §3c spec makes any single anchor reproducible; it says nothing about
whether one anchor's numbers are representative. This section runs the
identical canonical spec — scenario `standard`, seed 42, holdout 0.10,
only `--end-date` moving — across **7 pre-committed anchors spanning
exactly 3 weeks** (2026-08-07 → 2026-08-28, the canonical anchor
included), via `backend/scripts/run_multi_anchor.py`. Every run is stored
(run ids below; per-anchor dumps and the full analysis in
`ml/experiments/multi_anchor/`; run rows in
`backend/artifacts/multi_anchor/multi_anchor.db`). No anchor was tuned,
dropped, or re-run selectively; the content fingerprint over `backend/app`
+ the policy file was stable across the batch (`77dc23ba90e2b122`), and
the 2026-08-28 anchor reproduces the canonical spec's expected values
exactly (18/18, draw-derived ones included). Anchors are calendar
placements of the same generative process, not independent traffic: the
30-day windows overlap, so this measures window sensitivity, honestly
bounded — not generalization.

**What the anchor set controls.** Right edges a whole number of weeks
apart generate the *same dataset*, time-shifted and re-id'd (the
simulator's RNG is seeded by the seed alone and day-of-week quotas repeat
weekly; ids embed the `end_date`-keyed run id). Measured:
{08-07, 08-14, 08-28} are one dataset (68,410 events / 4,897 failed rows),
{08-18, 08-25} another (69,018 / 5,000), 08-10 and 08-22 unique — control
groups verified identical on events, failed rows, amounts, per-class mix,
and ground truth. Inside a control group every data-derived detection
number is bit-identical and every difference is draw re-keying; across
groups the calendar placement itself differs. The 7 anchors are therefore
4 distinct datasets × re-keyed draws — which is what lets the analysis
below separate *window* variance from *draw* variance.

<!-- MULTI-ANCHOR-TABLES-START -->

| anchor | run id | dataset version | wall |
|---|---|---|---:|
| 2026-08-07 | `run_0f7a34583dda43658d0ed51702a1efd7` | `sim_42_0269faaddc@2026-08-07` | 101s |
| 2026-08-10 | `run_f9435560796f412dae91dcb79b50f1de` | `sim_42_f2f831008d@2026-08-10` | 125s |
| 2026-08-14 | `run_c14f18d5ae2d472e9426c5063242f5bb` | `sim_42_cf55cc0d36@2026-08-14` | 94s |
| 2026-08-18 | `run_e073cb39d2994cfd88e829da437ecade` | `sim_42_b716d31f2a@2026-08-18` | 86s |
| 2026-08-22 | `run_e2db3506dffa4efc9d66c98a94e52d78` | `sim_42_7275c9dda3@2026-08-22` | 110s |
| 2026-08-25 | `run_7ecacdcf50cd42e58b3ce9ae94a70a15` | `sim_42_d78d99fa2d@2026-08-25` | 195s |
| 2026-08-28 | `run_9b86b04f86d64e628d36104731d052f1` | `sim_42_2b55f523ad@2026-08-28` | 250s |

| anchor | det P | det R | det F1 | MTTD (min) | matched (of 6) | unmatched rows | diag top-1 | diag top-3 (scored) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-08-07 | 0.333 | 0.667 | 0.444 | 230 | 4 | 8 | 1.000 | 1.000 (4) |
| 2026-08-10 | 0.385 | 0.833 | 0.526 | 563 | 5 | 8 | 0.800 | 1.000 (5) |
| 2026-08-14 | 0.333 | 0.667 | 0.444 | 230 | 4 | 8 | 1.000 | 1.000 (4) |
| 2026-08-18 | 0.714 | 0.833 | 0.769 | 635 | 5 | 2 | 0.800 | 1.000 (5) |
| 2026-08-22 | 0.444 | 0.667 | 0.533 | 230 | 4 | 5 | 1.000 | 1.000 (4) |
| 2026-08-25 | 0.714 | 0.833 | 0.769 | 635 | 5 | 2 | 0.800 | 1.000 (5) |
| 2026-08-28 | 0.333 | 0.667 | 0.444 | 230 | 4 | 8 | 1.000 | 1.000 (4) |

| anchor | opportunities | interventions | approvals | false int. (PR) | false int. (base) | unsafe | recovered, verified (paise) | baseline recovered (paise) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-08-07 | 1,424 | 100 | 95 | 6 | 421 | 0 | 2,258,400 | 100,041,200 |
| 2026-08-10 | 1,588 | 100 | 100 | 6 | 438 | 0 | 2,666,200 | 91,150,100 |
| 2026-08-14 | 1,433 | 100 | 95 | 8 | 421 | 0 | 2,187,500 | 98,345,100 |
| 2026-08-18 | 935 | 70 | 70 | 7 | 438 | 0 | 1,403,600 | 96,776,700 |
| 2026-08-22 | 1,090 | 90 | 90 | 4 | 415 | 0 | 1,455,300 | 96,761,200 |
| 2026-08-25 | 934 | 70 | 70 | 6 | 438 | 0 | 1,735,700 | 93,386,700 |
| 2026-08-28 | 1,457 | 100 | 94 | 8 | 421 | 0 | 3,670,700 | 92,398,100 |

| anchor | treatment rate | holdout rate | raw ITT lift, pp [95% CI] | class-adj lift, pp [95% CI] | realized holdout |
|---|---:|---:|---:|---:|---:|
| 2026-08-07 | 0.147 | 0.135 | +1.2 [-2.0, +4.0] | +1.2 [-1.7, +4.0] | 0.100 |
| 2026-08-10 | 0.144 | 0.148 | -0.5 [-3.9, +2.6] | -0.4 [-3.5, +2.7] | 0.101 |
| 2026-08-14 | 0.144 | 0.128 | +1.7 [-1.5, +4.3] | +1.6 [-1.2, +4.4] | 0.109 |
| 2026-08-18 | 0.146 | 0.159 | -1.3 [-4.8, +1.7] | -1.7 [-4.8, +1.5] | 0.105 |
| 2026-08-22 | 0.150 | 0.157 | -0.7 [-4.2, +2.4] | -1.2 [-4.5, +2.0] | 0.107 |
| 2026-08-25 | 0.142 | 0.154 | -1.2 [-4.9, +2.0] | -0.4 [-3.7, +2.8] | 0.091 |
| 2026-08-28 | 0.144 | 0.122 | +2.2 [-0.9, +5.0] | +1.5 [-1.5, +4.5] | 0.101 |

| incident kind | 08-07 | 08-10 | 08-14 | 08-18 | 08-22 | 08-25 | 08-28 | detected |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `gateway_degradation` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 7/7 |
| `method_outage` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 7/7 |
| `route_latency` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 7/7 |
| `customer_insufficient_funds_wave` | · | · | · | · | · | · | · | 0/7 |
| `checkout_abandonment_spike` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 7/7 |
| `subscription_failure_spike` | · | ✓ | · | ✓ | · | ✓ | · | 3/7 |

| metric | mean | min | max | stdev (sample) |
|---|---:|---:|---:|---:|
| detection precision | 0.465 | 0.333 | 0.714 | 0.175 |
| detection recall | 0.738 | 0.667 | 0.833 | 0.089 |
| detection F1 | 0.562 | 0.444 | 0.769 | 0.147 |
| MTTD (min) | 393 | 230 | 635 | 205 |
| matched incidents (of 6) | 4.428571 | 4 | 5 | 0.534522 |
| unmatched incident rows | 5.857143 | 2 | 8 | 2.853569 |
| diagnosis top-1 | 0.914 | 0.800 | 1.000 | 0.107 |
| diagnosis top-3 | 1.000 | 1.000 | 1.000 | 0.000 |
| opportunities | 1,265.857143 | 934 | 1,588 | 271.961657 |
| interventions (PulseCover) | 90.0 | 70 | 100 | 14.142136 |
| false interventions (PulseCover) | 6.428571 | 4 | 8 | 1.397276 |
| false interventions (baseline) | 427.428571 | 415 | 438 | 10.11364 |
| unsafe actions | 0.0 | 0 | 0 | 0.0 |
| recovered revenue, verified (paise) | 2,196,771.428571 | 1,403,600 | 3,670,700 | 794,585.878963 |
| baseline recovered (paise) | 95,551,300.0 | 91,150,100 | 100,041,200 | 3,289,597.454097 |
| failed payment rows | 4,921.0 | 4,845 | 5,000 | 57.859024 |
| treatment recovery rate | 0.145 | 0.142 | 0.150 | 0.003 |
| holdout recovery rate | 0.143 | 0.122 | 0.159 | 0.015 |
| raw ITT lift (pp) | +0.2 | -1.3 | +2.2 | +1.5 |
| class-adjusted lift (pp) | +0.1 | -1.7 | +1.6 | +1.3 |

<!-- MULTI-ANCHOR-TABLES-END -->

**What moves, and why.** Attribution is read from each anchor's stored
ground truth plus a detection probe (`--probe`) that re-runs the arm's
exact 12h/6h pass schedule per anchor and scores per-incident MTTD and
window composition; the probe's matched counts and mean MTTD reproduce
the stored runs on 7/7 anchors.

1. **Recall is per-kind stable, not per-window.** Four kinds
   (`gateway_degradation`, `method_outage`, `route_latency`,
   `checkout_abandonment_spike`) are found on 7/7 anchors, per-incident
   MTTD flat at 215/215/155/335 min. The recall swing (0.667–0.833) is
   exactly two kinds.
2. **`customer_insufficient_funds_wave` is a stable blind spot at the
   shipped operating point: 0/7** — despite being the largest injection
   on every anchor (133–193 injected failures; 56–61% of all failures in
   its 20h night window are insufficient-funds class). Mechanism, from
   `ml/experiments/detection/exp003`: the `insufficient_fund_share`
   signal flags only 1h buckets with share ≥ 0.90 at z ≥ 3 — floors set
   to keep organic 0.71-share daytime clusters out. The wave's failures
   spread across night-trough buckets and never concentrate into a
   near-single-class hour on these 7 windows. exp003 tuned and validated
   the signal on the 2026-08-27 anchor, where it *did* cross (§3b recall
   6/6). Counting that anchor, the kind is caught **1/8** — the clearest
   evidence in this section that a single-window detection headline
   overstates recall. The candidate follow-up named here — night-bucket
   floors — has since landed as an **opt-in, default-OFF** mode
   (`night_regime_floors` on the detection request: an all-night anomaly
   is judged by a 0.60 share / 15pp absolute floor set instead of the
   global 0.90 / 25pp; see docs/detection.md "Incident-level noise
   floors"). It ships dark precisely so this 0/7 reading and every other
   published number stay valid: no anchor above was re-run with it, and
   its only evidence so far is synthetic-fixture tests
   (`backend/tests/detection/test_night_regime_floors.py`) — disclosed,
   not tuned, exactly as this note promised.
3. **`subscription_failure_spike` is a knife-edge: exactly 5 injected
   failures over its 48h window on every anchor, found 3/7** (08-10,
   08-18, 08-25; 4/8 counting 2026-08-27). Window failure totals
   (301–340) do not separate found from missed — the within-window
   organic texture does; the kind sits at the detector's floor. When
   found, it is found **1,895–2,255 min** after injection start (the 5
   failures must accumulate), which fully decomposes the MTTD swing:
   (215+215+155+335)/4 = **230 min** on 4-match anchors;
   (920+1,895)/5 = **563**, (920+2,255)/5 = **635** on 5-match anchors.
   MTTD's variance is composition, not detection speed.
4. **Diagnosis top-3 is 1.000 on all 7 anchors; the only top-1 misses
   are one recurring mode.** On the 3 anchors where the sparse
   subscription window surfaces, the artifact labels it `no_fault`
   (confidence 0.52–0.54, true label rank 2) — the exp07 artifact's
   disclosed failure shape (docs/ml.md §10 tradeoffs; exp08
   re-verification), not an anchor effect. Every other matched incident
   is top-1 correct everywhere (top-1 1.000 on 4 anchors, 0.800 on the
   three 5-match anchors).
5. **Recovery economics are policy-pinned and draw-noised.** The gate's
   wall-clock brakes bind on every anchor: interventions 70–100 against
   934–1,588 opportunities (§1's rate-limit note), unsafe actions **0**
   on every anchor, holdout isolation 0/0 on every anchor, verified
   conversion on executed actions 25.7–38.0% vs the ~12–16% organic
   group rates — "the actions that execute do work" holds everywhere.
   Verified recovered revenue spans 1,403,600–3,670,700 paise — and the
   identical-dataset triple alone spans 2,187,500–3,670,700 (1.7×) on
   conversion-draw and holdout-exclusion re-keying only. A single run's
   gross recovery figure carries at least that much noise.
6. **The ITT lift point is window noise; the CI is the measurement.**
   Raw lift spans −1.3…+2.2 pp (class-adjusted −1.7…+1.6); **all 7 raw
   CIs bracket zero**; realized holdout 9.1–10.9%. §3's "the point
   estimate is noise in either direction" — inferred there from two
   anchors — is now measured across 7: at this policy envelope the
   fleet-level effect never resolves out of the ±5 pp band, and inside
   the identical-dataset triple the point still spans +1.2…+2.2 pp on
   draws alone.

**Bottom line.** The durable §3 claims survive every anchor: ~98%
intervention reduction vs the baseline (97.9–98.6%), false interventions
4–8 vs the baseline's 415–438, zero unsafe actions, 0/0 holdout
isolation, and the underpowered-lift band. The window-sensitive claims
are now quantified instead of anecdotal: recall 0.667–0.833 with the
per-kind matrix above, precision 0.333–0.714 (2–8 unmatched organic-noise
episodes per anchor), MTTD 230–635 min (composition), single-run
recovered revenue ±1.7× on draws alone. Reproduce: `cd backend &&
.venv/Scripts/python scripts/run_multi_anchor.py` (~20 min; `--probe`
adds the per-incident detection detail); verify:
`backend/.venv/Scripts/python ml/experiments/multi_anchor/cross_check.py`
(re-checks files ↔ stored rows ↔ this section's tables, and the
canonical-spec expected values).

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
  readings are stated and measured; the multi-anchor replay shipped in
  §3d quantifies the sensitivity across 7 anchors (per-kind recall is
  stable; the insufficient-funds wave is a stable blind spot at the
  shipped floors; precision swings 0.333–0.714 on organic-noise volume).
- **The dataset is calendar-day anchored unless pinned** (§1): "same
  seed" reproduces bit-for-bit within a calendar day; cross-day runs share
  the configuration, not the data. `run_evaluation.py --end-date` pins the
  anchor and the canonical spec (§3c) uses it — cross-day bit-
  reproducibility is shipped for runs made through the spec. The §3/§3b
  history predate the flag and stays as recorded.
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
# cross-day reproducible: pin the window anchor (the canonical spec, §3c)
.venv/Scripts/python scripts/run_evaluation.py --scenario standard --seed 42 --end-date 2026-08-28 --name canonical-v2
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

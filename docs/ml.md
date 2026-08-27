# ML Methodology — Incident Root-Cause Diagnosis

Owner: `backend/app/services/diagnosis/` · CLI: `backend/scripts/train_models.py` · Tests: `backend/tests/diagnosis/`

> **Status of all numbers on this page: PRELIMINARY / SYNTHETIC.** They are
> measured on the mini synthetic generator (`app/services/diagnosis/synthetic.py`),
> not on the full simulator (built in parallel) and not on real Razorpay data.
> The generator's fault signatures are deliberately clean and separable, so
> these results are **upper bounds**, useful for verifying the pipeline
> end-to-end — not evidence of production accuracy. A later wave retrains on
> full simulator output (`--input`) and updates this page.

## 1. Task

Given an incident raised by detection (a success-rate drop), classify the
root cause into the simulator-aligned taxonomy (`taxonomy.py`):

`gateway_degradation`, `route_latency`, `method_outage`, `bank_downtime`,
`abandonment_spike`, `subscription_failure_spike`,
`customer_insufficient_funds_wave`, `no_fault`.

The infrastructure-vs-customer-intent split (the first four vs the next
three) is what lets strategy route to *retry/route-around* vs
*nudge/payment-link* interventions; `no_fault` absorbs false-positive
detections instead of forcing a wrong cause.

## 2. Features

Unit of analysis: one **incident window** `[window_start, window_end)`
compared against an equal-length **baseline window** immediately before it.
Each payment in the window is normalized from `payment_events` ⨝ `payments`
(latest in-window event decides outcome: `failed` / `captured` / `pending` —
`pending` = no terminal state in-window, the abandonment proxy). Error
telemetry (`error_source` / `error_step` / `error_reason`) is read from the
event payload first, falling back to the payment columns, per Razorpay's
webhook payload taxonomy (docs/research.md). Enums are parsed defensively —
never strict-matched.

58 numeric features (`features.py::FEATURE_NAMES`), all plain floats
(JSON-serializable, stored verbatim on the diagnoses row):

- **Volume/rates:** volume, volume delta ratio, failure rates (window,
  baseline, delta).
- **Per-method / per-bank:** max within-group failure-rate delta, top-group
  share of window failures, one-hot of the top method, count of distinct
  failing banks (concentration vs spread discriminates method_outage from
  bank_downtime).
- **Error source/step/reason:** share-of-failures deltas and window shares.
  These dims exist only on failures, so within-group rates would be
  meaningless — shares are used instead. Per-value shares for the
  diagnostic reasons (`insufficient_fund`, `payment_timed_out`,
  `gateway_technical_error`, `bank_technical_error`, `incorrect_otp`,
  `payment_cancelled`, `card_declined`, `authentication_failed`,
  `transaction_limit_exceeded`).
- **Latency:** p50/p90 of `latency_ms` (when the source records it), absolute
  and relative deltas, plus a coverage feature so models can discount
  latency signals on sparse data.
- **Abandonment proxy:** pending-outcome rate and delta.
- **Subscriptions:** subscription traffic share, failure share/rate deltas.

## 3. Leakage controls

- **Temporal split, no shuffling.** Rows are sorted by `window_end` and cut
  into contiguous blocks: first 60% train, next 20% validation, last 20%
  test (`training.py::temporal_split`, fraction-validated). The test block
  is strictly in the future of everything the model was fit or selected on.
  `tests/diagnosis/test_diag_split.py` proves disjointness, chronological
  ordering, and per-class presence in every block.
- **Window-local features.** A row's features are computed only from events
  inside its own window and its immediately preceding baseline window — no
  row can contain information from another row's future.
- **Selection/reporting separation.** Model selection uses *validation*
  macro-F1; headline numbers are computed once on the untouched test block,
  with no train+val refit after selection.
- **Fit-time state.** The only fitted preprocessing is the logistic
  regression's `StandardScaler`, inside the sklearn `Pipeline`, fit on the
  training block only.
- Caveat: the synthetic generator draws i.i.d. regimes, so adjacent windows
  are near-duplicates in distribution. The temporal split still prevents
  *future* leakage, but simulator-retrained metrics (non-i.i.d., mixed
  causes) will be the meaningful ones.

## 4. Models compared (baselines first)

| Order | Model | Why |
|---|---|---|
| 1 | Multinomial logistic regression (scaled, class-weighted) | Interpretable baseline — per-feature odds ratios are inspectable; if it wins, we ship it |
| 2 | Random forest (200 trees, `min_samples_leaf=2`, class-weighted) | Non-linear interactions, robust to unscaled features |
| 3 | Gradient boosting (sklearn defaults) | Strong tabular learner; must beat the baselines to be selected |

Selection: max **validation macro-F1**, ties resolve to the earlier (simpler)
model — the comparison is real, not ceremonial: the winner is what
`backend/artifacts/diagnosis_active.json` points at. All three expose
`predict_proba`, giving the proba distribution and top-3 stored on every
inference.

## 5. Preliminary results (synthetic — see header caveat)

Command: `python scripts/train_models.py --synthetic --windows-per-class 200 --seed 42`
(1600 windows, 200/class; split 960/320/320; run 2026-08-26, model_version
`v20260826T182714Z-d54d647f`).

| Algo | Val macro-F1 | Val top-1 | **Test macro-F1** | **Test top-1** | **Test top-3** |
|---|---|---|---|---|---|
| **logistic_regression (selected)** | 0.9969 | 0.9969 | **0.9938** | **0.9938** | **1.0000** |
| random_forest | 0.9969 | 0.9969 | 0.9938 | 0.9938 | 1.0000 |
| gradient_boosting | 0.9969 | 0.9969 | 0.9938 | 0.9938 | 1.0000 |

Selected model per-class test metrics (support = 40/class):

| Class | P | R | F1 |
|---|---|---|---|
| gateway_degradation | 1.000 | 1.000 | 1.000 |
| route_latency | 1.000 | 1.000 | 1.000 |
| method_outage | 1.000 | 1.000 | 1.000 |
| bank_downtime | 1.000 | 1.000 | 1.000 |
| abandonment_spike | 1.000 | 1.000 | 1.000 |
| subscription_failure_spike | 1.000 | 0.950 | 0.974 |
| customer_insufficient_funds_wave | 1.000 | 1.000 | 1.000 |
| no_fault | 0.952 | 1.000 | 0.976 |

Confusion summary: the only test errors are 2/40 `subscription_failure_spike`
windows called `no_fault` (weak injections where only a handful of
subscription charges happened to land in the window).

Reading: all three algorithms tie — the synthetic signatures are linearly
separable, so the tie-break correctly ships the interpretable baseline. The
near-perfect numbers say "features and pipeline are sound", nothing more;
expect a real drop on simulator data with noise, mixed causes, and regime
drift.

**Heuristic fallback (cold start, no artifact):** 91.5% top-1 on the same
generator (439/480 windows, 60/class). Rules are deliberately strict —
failures bias toward `no_fault` rather than a confident wrong cause (the
dominant miss is weak `subscription_failure_spike` → `no_fault`; an
experiment with looser OR-thresholds tripled false-fires on other classes
and was rejected). Heuristic confidences are capped (≤0.7), explicitly
uncalibrated, and every heuristic row is flagged `heuristic=true`.

## 6. Artifacts, persistence, and inference

- **Artifacts** (gitignored): `backend/artifacts/diagnosis_<algo>_<version>.joblib`
  (pipeline + feature names + labels + metrics + sklearn version) and
  `diagnosis_active.json` (pointer: artifact file, model_version, selection
  scores). `model_version` = UTC timestamp + digest of
  (rows, seed, feature contract, taxonomy) for reproducibility tracing.
- **Experiment tracking:** each training run writes one `experiments` row
  (`config`: algos/split/dataset, `results`: full val+test metrics for all
  three algos, selection rule, model_version) and one `model_predictions`
  row **per test-set window** (input features, predicted/true label, proba,
  correctness) — the audit and offline-evaluation trail.
- **Inference:** `DiagnosisService.classify(incident_id)` computes features
  from the DB, loads the active artifact (heuristic fallback when absent),
  writes a `diagnoses` row (label, confidence, features JSON, explanation,
  version auto-incremented) and a companion `model_predictions` row with the
  structured output (full proba distribution, top-3, `heuristic` flag,
  rules fired). It also fills `incident.root_cause`; incident *status*
  transitions are left to the incident lifecycle owner.
- Schema note: the shared `diagnoses` table has no proba/top3/heuristic
  columns and this package does not edit shared models — that structured
  output lives on the companion `model_predictions` row, the table the
  architecture designates for "every model inference" (architecture §7).

## 7. Retraining on simulator output (next wave)

`python scripts/train_models.py --input <features.csv|parquet>` expects one
row per incident window: the 58 `FEATURE_NAMES` + `label` + `window_end`
(+ optional metadata). The simulator wave should either emit feature rows
directly (reusing `compute_features`, which is what `--synthetic` does) or
emit raw windows and let the same feature code transform them — do **not**
reimplement feature engineering in the simulator. Parquet needs `pyarrow`/
`fastparquet`, which is intentionally not in `requirements.txt`; CSV works
out of the box. Known gaps the simulator data will close: mixed/compound
causes, class imbalance, regime drift, noise on every signature, and
windows with sparse subscription traffic.

## 8. Final results on full simulator data

> Supersedes the PRELIMINARY caveat in the header for the active artifact.
> Trained on real simulator output (`app.simulator`) via
> `python -m app.services.evaluation.export_training` + `scripts/train_models.py --input`.

**Dataset** (`backend/artifacts/sim_features.csv`, reproducible): 2,050 labeled
feature windows from 60 simulator seeds, cycling the standard/storm/
upi_outage_demo/payday_wave_demo presets. Four distribution-coverage decisions,
each driven by a measured failure of the previous iteration:

- **Density diversity** — per-seed (days, events) cycle over
  (15d/30k, 10d/20k, 2d/60k, 5d/40k, 2d/30k, 7d/25k). A model trained only at
  ~2k events/day read the demo's 30k/day traffic as out-of-distribution and
  labeled a clear gateway degradation `subscription_failure_spike`.
- **Window-frame randomization** — each incident is emitted under its exact
  ground-truth span plus 4 random frames (pre-pad 0..90min or 0..8h,
  post-pad 0..60min or 0..8h, seeded), because at inference the feature window
  is the *detection analysis window*, not the injected span. Span-only training
  mislabeled real detection windows; fixed variants missed anchorings in
  between. Random frames teach the continuum.
- **Window staggering + placement jitter** — each seed's sim window is
  staggered 37h and incident day-fractions jittered ±0.08, otherwise identical
  fractions band whole classes into single temporal-split blocks (measured:
  support-0 classes in the held-out block on the first attempt).
- **Severity jitter** — incident strength is jittered per seed
  (fail_boost ×0.4–1.3 clamped to [0.08, 0.95], latency/abandon similar),
  because the presets only inject *strong* incidents; without mild examples
  the model interpolated a real 11-point degradation toward `no_fault`.

Labels: 1,150 positives over the 8-class taxonomy + 900 `no_fault` quiet
windows (1:1 with framed positives; `method_outage` with a targeted bank maps
to `bank_downtime`). Split: temporal by `window_end`, 1230/410/410
train/val/test, no shuffle, no leakage.

**Held-out (test block, used once):**

| algo | val macro-F1 | test macro-F1 | test top-1 | test top-3 |
|---|---:|---:|---:|---:|
| **logistic_regression (selected, val rule)** | **0.8444** | 0.8231 | 0.8780 | 0.9927 |
| random_forest | 0.8061 | 0.7962 | 0.9171 | 1.0000 |
| gradient_boosting | 0.8363 | **0.8320** | **0.9268** | 0.9927 |

Selected-artifact per-class test metrics (`logistic_regression`,
`v20260826T234303Z-c5434878` — the active pointer in
`backend/artifacts/diagnosis_active.json`):

| class | P | R | F1 | support |
|---|---:|---:|---:|---:|
| gateway_degradation | 0.976 | 0.911 | 0.943 | 45 |
| route_latency | 0.857 | 0.800 | 0.828 | 30 |
| method_outage | 0.865 | 1.000 | 0.928 | 45 |
| bank_downtime | 0.280 | 0.467 | 0.350 | 15 |
| abandonment_spike | 0.968 | 1.000 | 0.984 | 30 |
| subscription_failure_spike | 0.704 | 0.826 | 0.760 | 23 |
| customer_insufficient_funds_wave | 0.886 | 0.867 | 0.876 | 45 |
| no_fault | 0.963 | 0.876 | 0.917 | 177 |

**Honest caveats.** `bank_downtime` (single-bank outage from the storm
preset) remains the weakest class — a single bank's failure share is close
to organic noise at these volumes, so errors bias toward `no_fault` rather
than a confident wrong cause (the intended failure mode). The three algos
are within noise of each other; selection follows the documented
validation-F1 rule, not test-set cherry-picking. Numbers are on simulator
data with the simulator's failure taxonomy — real Razorpay traffic will be
noisier still.

**Post-training verification on fresh simulator incidents** (never in the
training frame): a fresh standard-preset run at an unseen seed+scale
(seed 777, 10d/25k) classifies **6/6 injected incidents top-1 correctly** on
their exact spans, confidences 0.998–1.0; the demo's 2-day
gateway-degradation window classifies `gateway_degradation` at confidence
1.0 on the real 180-minute detection window; the live `standard` preset's
subscription spike is classified `subscription_failure_spike` from its 24h
detection window via `GET /api/v1/incidents/{id}` (auto-diagnosis).
End-to-end scoring of detection-window diagnoses vs ground truth is in
docs/evaluation.md §2 (top-1 0.60 / top-3 0.80 on the diluted 12h windows
the scheduled passes produce — the gap vs exact spans is window dilution,
and motivates a window re-scoping triage step).

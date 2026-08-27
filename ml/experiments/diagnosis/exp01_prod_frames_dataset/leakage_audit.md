# exp01 — Leakage audit, class imbalance, and feature-contract assumption review

Dataset: `prod_frames_v1` (`backend/artifacts/prod_frames_v1.csv`, sha256 in
`config.json`; builder: `build_dataset.py`, 72 simulator seeds 5000–5071,
scenario cycle standard/storm/upi_outage_demo/payday_wave_demo, density cycle
(15d/30k, 10d/20k, 2d/60k, 5d/40k, 2d/30k, 7d/25k), customers 1500, severity +
placement jitter `prodframe-jitter:{seed}`, 37h/seed stagger; detection: real
`run_detection`, production defaults, 6h-step/12h-window schedule).

## 1. Leakage audit — labels and features cannot see each other

**Labels come ONLY from `simulator_ground_truth`.** Proof:

- The label path is `build_dataset.collect_seed_rows` →
  `app.services.evaluation.runner.load_ground_truth` (reads
  `SimulatorGroundTruth` rows, `entity_type="incident"`) → `truth_cause`
  (`KIND_TO_CAUSE`; method outage + targeted bank → `bank_downtime`) →
  `app.services.diagnosis.prodframe.label_detection_window` (span-overlap
  rule, `LABELING_RULE_VERSION = prodframe-label-v1`).
- `grep -rn "SimulatorGroundTruth" backend/app/services/diagnosis/` → **no
  matches**: the diagnosis package (features, training, service) never
  touches the ground-truth table. The only reference in the repo's diagnosis
  code paths is the dataset builder under `ml/experiments/diagnosis/`, which
  is composition code, not serving code.
- Labels are assigned from span overlap only; no feature value is an input
  to the labeling rule.

**Features come ONLY from `payment_events` ⨝ `payments`.** Proof:

- The only DB read in feature computation is
  `features.py:144` (`sa.select(Payment, PaymentEvent)` over the window);
  `compute_features` is pure over those records. The same function is used
  for training rows and for inference (`compute_features_for_incident`), so
  there is no train/serve feature skew by construction.
- No feature reads `simulator_ground_truth`, `incidents` metadata, or any
  diagnosis/strategy output. `DiagnosisService` reads `Diagnosis.version`
  only to version its own rows, never for features.

**Temporal leakage controls** (unchanged, docs/ml.md §3): rows sorted by
`window_end`, contiguous 60/20/20 blocks, no shuffling; each row's features
use only its window + the immediately preceding equal-length baseline;
calibration CV is time-aware (`TimeSeriesSplit` inside the training block).

**Residual, disclosed: episode adjacency.** Scheduled passes half-tile the
timeline, so one injected incident can yield multiple persisted rows (e.g. a
`payment_success_rate` and a `capture_latency_ms` incident for the same
episode, windows 6h apart sharing half their events). Such near-duplicate
rows can straddle a split boundary — a mild optimism channel the temporal
block split cannot fully close at this dataset size. Mitigations: blocks are
still strictly future-disjoint; severity/placement/stagger jitter decorrelates
episodes across seeds; and the honest frame is small because production
produces few rows per incident. Measured effect is bounded by the val→test
generalization gap reported in exp03.

## 2. Class-imbalance analysis

Authoritative numbers in `dataset_summary.json` (build of 2026-08-27, 72
seeds): **259 rows** — `no_fault` 95 (**36.7%**), `method_outage` 55,
`customer_insufficient_funds_wave` 35, `gateway_degradation` 24,
`subscription_failure_spike` 23, `abandonment_spike` 15, `route_latency` 8,
`bank_downtime` 4; 70/259 rows (27.0%) overlap more than one ground-truth
incident (storm-preset concurrency + long anomaly spans; labeled by largest
overlap). Per-scenario rows: storm 103, payday 58, standard 49,
upi_outage_demo 49.

- The `no_fault` share is set by **detection false positives only** (persisted
  incidents with zero ground-truth overlap), not by sampled quiet windows as
  in the exact-span dataset (docs/ml.md §8, 900/2050 = 44%). The post-redesign
  admission floors (docs/detection.md) filter most organic-noise fires, so
  this class is smaller here — an intended distribution shift: production
  only ever serves windows detection admitted.
- Positive classes are limited by **detection recall**: incidents the floors
  or the zscore detector miss (mild severity-jittered injections,
  `route_latency` barely moving merchant-wide p90, abandonment waves that
  never reach terminal outcomes) produce no rows. That is production reality —
  diagnosis cannot classify what detection never raises — and it caps the
  support of the harder classes (`bank_downtime` 4 rows, `route_latency` 8:
  per-class metrics on these supports are indicative, not precise, and are
  always reported with their support).
- `class_weight="balanced"` (LR) / `balanced_subsample` (RF) handle the
  skew; per-class supports are reported with every metric set.

## 3. Unrealistic-assumption review — the 58-feature contract

Assumptions baked into `features.py` that the production-frame data stress-tests:

1. **Equal-length baseline immediately preceding the window.** On 12h
   production windows the baseline is the previous 12h — fine for day-scale
   incidents, but a 20–48h incident (insufficient-funds wave, subscription
   spike) spills INTO the baseline of the *next* pass's window, shrinking
   measured deltas for later passes of the same episode. Deltas are computed
   against a contaminated baseline.
2. **"Latest in-window event decides outcome; pending = abandonment proxy."**
   On diluted 12h windows, slow-but-healthy traffic sits in `pending`
   transiently, inflating `abandonment_rate_w` for dense windows — a
   systematic confound for `abandonment_spike` vs high-traffic quiet windows.
3. **Latency features assume coverage.** `latency_p50/p90` default to 0.0
   with a separate `latency_coverage` feature; models must learn the
   interaction. On low-latency-coverage windows (wallet/emi traffic) the
   latency block is noise.
4. **Concentration features assume one dominant cause.** `max_*_delta` /
   `top_*` one-hots encode a single-cause world; storm-preset compound
   incidents (multi-overlap rows, counted in `dataset_summary.json`) are
   labeled by largest overlap but their features are mixtures — an irreducible
   error source for those rows, documented in failure analysis.
5. **Volume features are absolute.** `volume`, `failed_volume` are raw counts;
   the density cycle (2k–30k events/day) forces the model to learn density
   invariance rather than assume a fixed merchant scale — kept deliberately,
   since production serves all densities.
6. **Share features are 0 on failure-free windows.** Quiet 12h windows with
   ~0 failures make every share/delta feature 0 — the `no_fault` signature is
   "everything zero", separable but brittle to a handful of organic failures.

None of these invalidates the contract (it is JSON-serializable, deterministic,
inference-identical); they are the known channels through which diluted
production windows are harder than exact spans, and they structure the
failure analysis in exp03.

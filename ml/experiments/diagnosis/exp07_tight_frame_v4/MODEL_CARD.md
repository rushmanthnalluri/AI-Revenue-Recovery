# Model card — diagnosis_random_forest v20260828T013109Z-77a4ef3b (ACTIVE)

Shipped 2026-08-28 by exp07 (SHIP_VERDICT.md). Supersedes
`logistic_regression v20260826T234303Z-c5434878` (kept in
`backend/artifacts/` for rollback: re-point `diagnosis_active.json`).

## Identity

- Algo: `random_forest` raw (200 trees, `min_samples_leaf=2`,
  `class_weight=balanced_subsample`, seed 42) — no calibration wrapper.
- Feature contract: the 58 `FEATURE_NAMES` (app/services/diagnosis/features.py)
  — unchanged since §8; taxonomy: the 8 `CAUSES` — unchanged.
- sklearn 1.9.0; artifact payload carries labels, feature names, metrics.

## Training data (v4b2, 3254 rows, per-source temporal 60/20/20, no shuffle)

- `prod_frames_v3` (644): one row per PERSISTED scheduled-pass detection
  incident (12h lookback, 6h step, production floors+dedup) on the stabilized
  detection engine; 144 seeds (6000–6143) x 4 presets x 7-point density cycle
  topping at 70k events/day; severity/placement jitter; prodframe-label-v1.
- `tight_frames_v1` (435): ad-hoc tight frames — demo-shape (success-rate,
  10-min buckets, 180/240-min, anchored gt_end+25min) + all-metric 180-min
  passes; dedup disabled during collection (each row = the incident production
  persists if that pass runs on fresh state); same labeling rule.
- `sim_features` (2050): the §8 exact-span dataset (unchanged, seeds 1000–1059).
- `aug_pure_sr_v1` (125, TRAIN-ONLY): latency_multiplier=1.0 gateway
  degradations (fail_boost U(0.08,0.35)) at 70k/30k events-day, seeds
  7000–7035 — the measured demo-A OOD hole (pure success-rate drop).
- prod_frames_v2 NOT used (contaminated shard2, exp04) — held-out reference.

## Held-out performance (test blocks; incumbent = previous active LR)

| frame | macro-F1 | top-1 | top-3 | ECE | safe auto-lane | unsafe | false-fire |
|---|---:|---:|---:|---:|---:|---:|---:|
| prod_v3 (n=130) | 0.6293 (inc 0.4991) | 0.7154 (0.5385) | 0.9308 | 0.1291 (0.3332) | **0.2708** (0.1906) | **0.0928** (0.567) | **0.0206** (0.134) |
| tight (n=87) | 0.5991 (0.5783) | 0.8506 (0.8046) | 0.9655 | 0.0524 (0.1741) | **0.4348** (0.1352) | **0.3333** (0.778) | **0.1111** (0.333) |
| exact span (n=410) | 0.7664 (**0.8231**) | **0.9098** (0.8780) | 0.9951 | 0.1465 (**0.0510**) | **0.2537** (0.1915) | **0.3082** (0.666) | **0.0000** (0.023) |
| prod_v2 legacy (n=102) | 0.6104 (0.4375) | 0.7451 (0.5686) | 0.8824 | 0.1416 (0.2683) | 0.1333 (0.0055) | 0.1667 (0.528) | 0.0139 (0.181) |

Business metric: threshold 0.85 (policy auto floor); auto classes =
gateway_degradation / method_outage / bank_downtime.
Demo operating points (real demo frames): A 0.974 (floor 0.867), B 1.000,
D 1.000 (floor 0.944) — all top-1 gateway_degradation.

## Intended use / limits

- Serves DiagnosisService.classify on detection-produced incident windows —
  BOTH scheduled 12h passes and ad-hoc tight windows (180/240-min), the two
  production frame families, plus exact spans.
- Known weaknesses (disclosed): exact-span macro-F1 −0.057 vs incumbent
  (bank_downtime 0.000 F1 at support 15, subscription_failure_spike 0.516 at
  23 — thin classes; confusions stay in-lane or bias to no_fault);
  auto_coverage 0.364 (hedges more recoverable incidents into the approval
  lane — slower, never unsafer); span ECE 0.1465 (raw RF votes are less
  calibrated on exact spans; isotonic variants failed the financial-safety
  clauses — see metrics_v4b2.json).
- bank_downtime remains the weakest class (9–20 training rows): errors bias
  to no_fault/method_outage, the intended safe failure mode.

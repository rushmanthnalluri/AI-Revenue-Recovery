# MODEL CARD — diagnosis random_forest+isotonic v20260827T162752Z-1781945e (NOT DEPLOYED)

**Status: CANDIDATE — closest to shippable in the loop, rejected on the demo
scenario-D operational check (misses the required confidence by 0.034). The
active pointer remains `logistic_regression v20260826T234303Z-c5434878`.**
This card documents the candidate fully so the lead can revisit it (e.g.
after the scoped exp07 ad-hoc-frame augmentation, or a demo retune).

## Identity

- Model: `CalibratedClassifierCV(RandomForestClassifier(n_estimators=200,
  min_samples_leaf=2, class_weight="balanced_subsample", random_state=42),
  method="isotonic", cv=TimeSeriesSplit(3))` — time-aware calibration folds.
- model_version `v20260827T162752Z-1781945e`; trained 2026-08-27; git sha
  `437dac6` (+ diagnosis-track working-tree changes); sklearn 1.9.0; feature
  version `aa36be6f` (58-feature contract).

## Data

`dual_frame_v3` (2556 rows): `prod_frames_v2` (506 persisted detection
windows, seeds 5000–5143) + `sim_features.csv` (2050 exact-span frames, §8),
per-source temporal 60/20/20 split (train 1533 / val 511 / test 512) —
exp05's prod test block (n=102) and §8's exact-span test block (n=410)
remain exactly held out.

## Metrics (held-out)

| frame | macro-F1 | top-1 | top-3 | ECE | Brier | safe | auto | unsafe | false-fire |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| prod detection windows (n=102) | 0.5431 | 0.6863 | 0.8529 | 0.1536 | 0.4789 | **0.2194** | 0.733 | 0.5139 | 0.0278 |
| exact-span frames (n=410) | 0.8026 | **0.9049** | 0.9878 | 0.0618 | 0.1715 | 0.2548 | 0.829 | 0.5738 | 0.0000 |
| combined val (n=511) | 0.7612 | 0.8806 | 0.9765 | 0.0365 | 0.1856 | 0.0278 | 0.674 | 0.6461 | 0.0027 |

Beats the old artifact on its own exact-span frame (top-1 0.9049 vs 0.8780)
AND on every prod-frame gate clause (safe +0.214, unsafe −0.014, macro-F1
+0.106, false-fire 0.028 vs 0.181).

## Why rejected (measured, `candidate_frames.json` / `SHIP_VERDICT.md`)

Demo scenario D's auto lane needs diagnosis confidence >= 0.944 (soft-decline
pick, action_fit 0.90, floor 0.85): this model scores 0.910 on that frame
(demo A: 0.941 — passes). The only candidate clearing both demo bars
(gradient_boosting raw, 0.977/0.998) fails the prod-gate unsafe clause
(0.7083 > 0.5278). Shipping it would red the demo suite; the mission bars
that.

## Risks

Strict unsafe_coverage 0.5139 on prod frames is only marginally better than
the old artifact's (the false-fire subset that actually reaches auto-exec is
6.5x better: 0.0278 vs 0.1806). Isotonic calibration on ~500-row folds can
shift under retraining; recheck both demo frames on any retrain.

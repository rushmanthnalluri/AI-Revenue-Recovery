# MODEL CARD — diagnosis random_forest v20260827T123306Z-609981e3 (v1 candidate)

**Status: INTERMEDIATE CANDIDATE — not deployed.** Trained and selected on
`prod_frames_v1` (259 rows, 72 seeds); superseded by the exp05 v2 run before
any deploy decision, because v1's held-out blocks (val 51 / test 53) were too
small to resolve the business metric. Kept as a full record of the v1 run.

## Identity

- Model: `RandomForestClassifier(n_estimators=200, min_samples_leaf=2,
  class_weight="balanced_subsample", random_state=42)`, raw (no calibration
  wrapper — won the pre-registered validation rule among 9 candidates:
  LR/RF/GB x raw/sigmoid/isotonic, time-aware calibration CV).
- model_version `v20260827T123306Z-609981e3`; trained 2026-08-27; code git
  sha `437dac6` (+ uncommitted diagnosis-track changes); sklearn 1.9.0.
- Artifact: `artifacts/diagnosis_random_forest_v20260827T123306Z-609981e3.joblib`
  (this directory; NOT the production pointer).

## Data

- Train/val/test: 155/51/53 — temporal blocks by `window_end`, no shuffle —
  of `prod_frames_v1` (sha256 in `config.json`): one row per persisted
  detection incident from 72 simulator seeds (5000–5071), production
  detection config, labeling rule `prodframe-label-v1`.
- Feature contract: 58 features, feature_version `aa36be6f` (see
  `config.json`), identical code path at train and serve.

## Intended use / out of scope

Classify the root cause of a *detection-raised incident window* into the
8-class taxonomy; confidence feeds strategy confidence (x action-fit, 0.85
auto-execute floor). Not for windows detection did not raise; not for
non-simulator traffic without revalidation.

## Metrics (held-out test block, n=53 — small; see exp05 for the decision run)

macro-F1 0.3826 · top-1 0.6038 · top-3 0.8679 · macro-FPR 0.0624 · ECE
0.1357 · Brier 0.5557 · safe auto-lane coverage 0.1959 (auto 0.25 / unsafe
0.0541 / false-fire 0.0). Full per-class P/R/F1 with supports in
`metrics.json`; failure modes in `failure_analysis.md`.

## Risks

Covers only 25% of auto-recoverable incidents above the auto-execute floor
(conservative); `subscription_failure_spike` unreadable on diluted windows
(0/7); per-class supports as low as 2 make rare-class metrics indicative
only. Calibration (ECE 0.136) is materially better than the previously
deployed artifact (0.282) on the same block.

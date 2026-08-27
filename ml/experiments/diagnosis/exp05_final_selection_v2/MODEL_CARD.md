# MODEL CARD — diagnosis random_forest v20260827T125532Z-b20a153f (v2 candidate)

**Status: REJECTED AFTER OPERATIONAL REVIEW — not active.** Was deployed
2026-08-27 on the exp05 gate, then rolled back the same day: trained only on
12h scheduled windows, it hedges on the demo's tight ad-hoc windows
(gateway_degradation 0.239 on scenario D's 180-min frame vs the old
artifact's 1.000 — measured), breaking the demo's auto lane. Superseded by
the exp06 dual-frame work; see `../exp06_dual_frame_v3/SHIP_VERDICT.md`.
The prod-frame metrics below remain the best production-frame numbers of the
loop.

## Identity

- Model: `RandomForestClassifier(n_estimators=200, min_samples_leaf=2,
  class_weight="balanced_subsample", random_state=42)`, raw probabilities
  (no calibration wrapper — raw RF won the pre-registered validation rule
  among 9 candidates: LR/RF/GB x raw/sigmoid/isotonic, time-aware
  `TimeSeriesSplit(3)` calibration CV).
- model_version `v20260827T125532Z-b20a153f`; trained 2026-08-27; git sha
  `437dac6` (+ diagnosis-track working-tree changes); sklearn 1.9.0; feature
  version `aa36be6f` (58-feature contract, identical code path train/serve).

## Data

- `prod_frames_v2` (sha256 `c8d812bb…f502b56d`; 506 rows, 144 simulator
  seeds 5000–5143, two build shards): one row per PERSISTED detection
  incident — the diluted 12h windows production actually serves — labeled by
  `prodframe-label-v1` from simulator ground truth. Split: temporal by
  `window_end`, 303/101/102 train/val/test, no shuffle, no refit after
  selection.
- Label mix: no_fault 37.8%, method_outage 103, insufficient_funds 70,
  gateway 47, subscription 45, abandonment 22, route_latency 19,
  bank_downtime 9.

## Metrics (held-out test, n=102 — production frames)

macro-F1 0.5793 · top-1 0.7647 · top-3 0.9314 · macro-FPR 0.0398 · ECE
0.2007 · Brier 0.4326 · **safe auto-lane coverage 0.1889** (auto 0.30 /
unsafe 0.111 / false-fire 0.014). Per-class P/R/F1 + supports:
`training/metrics.json`; confusion: `training/confusion_matrix.csv`.

Exact-span continuity (docs/ml.md §8 frames, test n=410): macro-F1 0.4604 ·
top-1 0.6707 · top-3 0.9390 · safe 0.3094 — less accurate than the old
span-trained artifact ON THE EASY FRAME by design (it is trained on diluted
windows), still safer by the business metric there.

## Intended use / limits

- Classify detection-raised incident windows into the 8-class taxonomy;
  confidence (max proba) feeds strategy confidence (x action-fit, 0.85
  auto-execute floor). Only 30% of auto-recoverable incidents cross the floor
  — the model is deliberately conservative; most incidents take the
  human-approval lane.
- Weak classes: abandonment_spike (dense-window pending confound),
  bank_downtime + route_latency (tiny supports). Multi-incident windows cap
  at ~0.59 accuracy. Fails toward no_fault, not toward confident wrong
  causes (2/102 overconfident-wrong, 1 actionable).
- Simulator distribution; not validated on real Razorpay traffic.

## Risks

Financial-safety relevant: residual false-fire rate 1.4% of non-auto
incidents at >=0.85 (vs 18.1% for the previous artifact). Revisit
`random_forest+isotonic` at larger frame scale (it traded more auto coverage
for more unsafe crossings; lost on validation here).

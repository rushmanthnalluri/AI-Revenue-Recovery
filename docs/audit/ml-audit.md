# ML Audit — PulseRecover (Audit Phase 10)

Captured: 2026-09-02. Auditor: AI/ML audit agent (read-only on models/artifacts; no production code modified).
Verdict vocabulary per published metric: REPRODUCIBLE / CURRENT / STALE / UNDERPOWERED / CHERRY-PICKED / HONEST / MISLEADING.

Scope: the incident root-cause diagnosis model (the only trained ML in the
repo), its data/labels/features/splits/artifacts/calibration, the experiment
record chain under `ml/experiments/`, and the published metrics docs quote.

## 1. Training data (simulator ground truth)

All training data is simulator-generated; there is no real production traffic
in any training set (Razorpay test-mode account cannot produce incident-scale
data — baseline notes detection consumes a webhook EVENT stream real_test
barely has).

Active model's dataset (**v4b2, 3,254 rows** — `backend/artifacts/diagnosis_active.json:8`,
`ml/experiments/diagnosis/exp07_tight_frame_v4/MODEL_CARD.md:15-29`):

| source | rows | what |
|---|---:|---|
| `prod_frames_v3` | 644 | one row per PERSISTED scheduled-pass detection incident (12h lookback, 6h step, production floors), stabilized detection engine, 144 seeds (6000–6143), density to 70k events/day |
| `tight_frames_v1` | 435 | ad-hoc tight frames (demo-shape 180/240-min success-rate + all-metric 180-min passes, anchored gt_end+25min, dedup disabled during collection) |
| `sim_features` | 2050 | the docs/ml.md §8 exact-span dataset (seeds 1000–1059) |
| `aug_pure_sr_v1` | 125 | TRAIN-ONLY latency_multiplier=1.0 gateway degradations (seeds 7000–7035) — added after a measured OOD failure on demo A (0.635 → 0.974; SHIP_VERDICT.md:22-36) |

`prod_frames_v2` (506 rows) was never trained on — its shard2 was built while
detection code was mid-edit (disclosed in
`ml/experiments/diagnosis/exp04_prod_frames_v2/CONTAMINATION.md:13-31`, ~70
rows ≈ 14% affected); it is kept as a held-out legacy reference frame.

Dataset CSVs live in `backend/artifacts/` (gitignored per README:46-49);
sha256-pinned in experiment configs and re-hashed before re-scoring in exp08
(`exp08_hardening_review/REVIEW_VERDICT.md:11-16`).

## 2. Labels

- Labels come ONLY from `simulator_ground_truth` via
  `load_ground_truth` → `GroundTruthSpan` → `label_detection_window`
  (labeling rule `prodframe-label-v1`,
  `backend/app/services/diagnosis/prodframe.py:49,98-125`): span-overlap
  matching; no overlap → `no_fault`; multiple overlaps → largest-overlap
  cause with deterministic tie-breaks; all overlapping ids kept on the row
  for audit (prodframe.py:30-34).
- Leakage boundary independently re-verified by this audit: grep for
  `SimulatorGroundTruth|simulator_ground_truth|ground_truth` in
  `backend/app/services/diagnosis/` matches ONLY `prodframe.py` (the pure
  labeling contract; no DB/simulator imports — prodframe.py:36-38). Features
  never read ground truth; labels never read features
  (`exp01_prod_frames_dataset/leakage_audit.md:10-37`).
- Label set: 8 causes (`backend/app/services/diagnosis/taxonomy.py:12-27`),
  aligned with the simulator's injection taxonomy so diagnoses score against
  ground truth.
- Known label-quality limits (disclosed, leakage_audit.md:55-81): prod-frame
  class balance is set by detection recall + false-positive rate, not by
  sampling — `bank_downtime` 4 rows, `route_latency` 8 in v1; 70/259 v1 rows
  overlap >1 ground-truth incident (storm concurrency), labeled by largest
  overlap — irreducible label noise on those rows, disclosed.

## 3. Features (diagnosis/features.py)

- 58 features (`backend/app/services/diagnosis/features.py:73-119`; count
  verified by import: `len(FEATURE_NAMES) == 58`), computed from
  `payment_events ⨝ payments` only (features.py:137-184 — the only DB read).
- Shape: volume/headline rates; per-method/per-bank within-group failure-rate
  deltas; error source/step/reason share-of-failures deltas; latency
  p50/p90 deltas + coverage; abandonment proxy (pending-without-terminal
  state); subscription mixes (conventions documented features.py:24-29).
- Train/serve skew: none by construction — the same `compute_features`
  serves training rows and inference (`compute_features_for_incident`,
  features.py:205-227).
- Known assumption weaknesses are documented and stress-tested
  (leakage_audit.md:83-117): equal-length preceding baseline contaminated by
  20–48h incidents; pending-as-abandonment confound on dense windows;
  latency coverage; single-cause concentration features vs storm compounds;
  absolute volume features; zero-signature brittleness of `no_fault`.

## 4. Split strategy (temporal? leakage controls?)

- **Temporal, contiguous, no shuffling**: rows sorted by `window_end`, cut
  into 60/20/20 train/val/test blocks (`training.py:146-163`). Multi-source
  frames use per-source presplit so each source's held-out block stays the
  block earlier experiments reported against (training.py:430-458).
- **Calibration CV is time-aware**: `CalibratedClassifierCV(cv=TimeSeriesSplit(3))`
  inside the training block (training.py:105-115, 60-72).
- **Selection is pre-registered** (training.py:80-97): validation
  safe_auto_lane_coverage → validation macro-F1 → validation ECE →
  baseline-first order (LR > RF > GB, raw > sigmoid > isotonic). Test block
  touched once, after selection, no refit (training.py:13-15).
- **Residual leakage channel, disclosed**: episode adjacency — one injected
  incident can yield multiple persisted rows sharing events; such
  near-duplicates can straddle a split boundary (leakage_audit.md:44-53).
  Bounded, not eliminated; mitigations listed there.
- **v4b merge bug (caught, corrected, kept on record)**: an intermediate
  exp07 run merged the 125 aug rows pre-split, leaking them into held-out
  blocks; the block-integrity check caught it; corrected to TRAIN-ONLY
  (v4b2); invalid files kept as the record
  (`exp07_tight_frame_v4/SHIP_VERDICT.md:32-36,124-126`;
  `metrics_v4b.json` exists alongside `metrics_v4b2.json`).

## 5. Artifacts (active model via diagnosis_active.json)

- Active pointer `backend/artifacts/diagnosis_active.json` →
  `diagnosis_random_forest_v20260828T013109Z-77a4ef3b.joblib`
  (random_forest raw, calibration "none", trained_at 2026-08-28T01:31:09Z,
  dataset "v4b"; pointer metrics: val F1 0.7401, test F1 0.6293, test top-1
  0.7154, top-3 0.9308, ECE 0.1291, safe 0.2708). The referenced joblib file
  exists in `backend/artifacts/` (directory listing verified).
- Rollback incumbent `logistic_regression v20260826T234303Z-c5434878` also
  present in `backend/artifacts/` (12 historical artifacts total + the
  pointer + `diagnosis_train_frame.csv`, the 1600-row §5/§8 synthetic frame —
  1601 lines incl. header, verified).
- Loading contract: missing pointer or missing file → `None` → heuristic
  fallback, never a crash (`training.py:592-602`, `service.py:92-97`).
- Inference path: artifact `predict_proba` aligned to canonical label order,
  top-3 + full proba persisted on a `model_predictions` row with the feature
  vector — every inference auditable (`service.py:174-196, 118-135`).
- exp08 hardening review (2026-08-28) re-scored the shipped bytes through
  the active pointer on sha256-pinned datasets: every leaf number reproduces
  `ship_metrics.json` at 4dp; gate re-passes with both sides re-measured
  (`exp08_hardening_review/REVIEW_VERDICT.md:10-38`).

## 6. Calibration

- The shipped model is **raw (uncalibrated) random forest** — calibrated
  variants (sigmoid/isotonic) were trained for every algo but lost the
  financial-safety gate (SHIP_VERDICT.md:128-130: GB+sigmoid was the val-rule
  winner but failed prod unsafe 0.206 and demo A 0.717).
- Measured ECE on held-out blocks: prod_v3 0.1291, tight 0.0524, exact-span
  0.1465 (metrics_v4b2.json, re-verified exp08). Brier 0.459 prod.
- Risk framing is explicit (training.py:60-68): strategy confidence =
  diagnosis confidence × action-fit (≤0.98), gated at 0.85, so overconfidence
  is an unsafe-automation risk. Mitigations in serving code, not in the
  model: the agent layer caps gate-input confidence at 0.84 for
  non-auto-recoverable classes (reasoners.py:147-156) and the policy gate
  independently enforces floors (policies/default.yaml:37-50).
- Heuristic fallback confidences are capped ≤0.7 and explicitly uncalibrated
  (`heuristic.py:21-24, 91-97`) — measured effect: heuristic unsafe_coverage
  0.0 on prod frames (exp02 metrics.json: it can never cross the floor).

## 7. Eval scripts + ml/experiments/**/metrics.json

- Training CLI: `backend/scripts/train_models.py` (referenced by
  `ml/experiments/diagnosis/README.md:48-49`; drivers `run_v4.py`,
  `build_dataset.py`, `build_aug_pure_sr.py`, `ship_candidate.py` in exp07).
- Agent eval: `backend/scripts/agent_eval.py` (corpus `agent-corpus-1.1`,
  38 cases; see docs/audit/ai-audit.md §3.6 — records match code).
- Diagnosis records read for this audit: exp01 (leakage audit + 259-row v1),
  exp02 baselines (prod-frame baseline numbers), exp03 (calibration
  candidates, n≈52 unresolvable → scale-up), exp04 (v2 + contamination
  disclosure), exp05 (v2 DEPLOY → same-day ROLLBACK, superseded banner on
  DECISION.md:3-10), exp06 (dual-frame v3, NO-SHIP constraint map), exp07
  (v4/v4b/v4b2 iterations, SHIP), exp08 (read-only re-verification, KEEP).
  Multi-anchor: `ml/experiments/multi_anchor/aggregate.json` (7 anchors,
  diagnosis top-1 mean 0.914 min 0.8 max 1.0, top-3 1.000 everywhere).
- Live probes: exp07 seed-777 TestClient check 6/7 top-1 (`live_check.log`);
  exp08 fresh-seed probes: seed 888 2/4, seed 1234 8/11 — combined 10/15,
  every miss hedged ≤0.6028, every answer ≥0.80 correct
  (`exp08_hardening_review/REVIEW_VERDICT.md:49-65`).
- Honesty culture observed across records: invalid runs kept (v4b),
  superseded decisions kept unedited (exp05), contamination disclosed
  (exp04), failed gate readings disclosed at ship time (exp07), weaknesses
  re-confirmed not fixed (exp08).

## 8. Verdict per published metric

### 8.1 Top-1 accuracy 1.000 on 4 windows

Claim (docs/evaluation.md:234-245; README/claim-matrix 4.3): the exp07 RF
artifact labels all 4 matched detection windows of the 2026-08-28 canonical
run correctly (confidences 0.48/0.98/0.60/0.76).

- REPRODUCIBLE — prior audit re-run matched (claim-matrix.md:112);
  multi_anchor shows 1.000 on 4 of 7 anchors, 0.800 on 3
  (`multi_anchor/aggregate.json` diagnosis_top1 mean 0.914).
- UNDERPOWERED as a standalone headline — n=4 on the canonical anchor; the
  doc itself supplies the denominator honesty (the two incidents detection
  missed drop out of the scored set — survivorship disclosed at
  evaluation.md:238-241) and explicitly says the case for the swap rests on
  the exp07 gate, "not on this table" (evaluation.md:242-245).
- HONEST — small-n caveat in place; cross-anchor mean 0.914 published
  (evaluation.md:537).
- NOT misleading **when quoted with its n and anchor context**; quoting
  "1.000" bare would be cherry-picking the best anchor.

### 8.2 exp07 gate (unsafe 0.0928 ≈ "9.3%", safe 0.2708, F1 0.6293, span top-1 0.9098)

Claim (docs/ml.md §10:423-448; exp07 SHIP_VERDICT.md; claim-matrix 6.9):
RF v4b2 beats the incumbent on every hard clause; deployed as active.

- REPRODUCIBLE — this audit independently re-read
  `exp07_tight_frame_v4/metrics_v4b2.json`: RF prod_v3_test macro-F1 0.6293 /
  top-1 0.7154 / ECE 0.1291 / safe 0.2708 / unsafe 0.0928 / false-fire 0.0206
  (n=130); incumbent LR same-block 0.4991 / 0.1906 / 0.567 — matches
  SHIP_VERDICT and MODEL_CARD cell-for-cell; exp08 re-measured at 4dp.
- CURRENT — the shipped artifact is still the active pointer (verified
  §5), unchanged since 2026-08-28.
- HONEST, with one governance caveat worth a Principal's attention: the
  machine-recorded gate in `metrics_v4b2.json` reads
  `gate.span_continuity: false, gate.all: false, gate_passers: [],
  ship_candidate: null` — under the stricter pre-registered span macro-F1
  operationalization (≥ incumbent − 0.03), RF FAILS (0.7664 < 0.7931). The
  ship proceeded under the campaign's older exp06 clause (span top-1), and
  the conflict is disclosed in prose in SHIP_VERDICT.md:98-103 and
  ml.md:454-458. So "passes every gate clause" is true only under the exp06
  reading; the record shows a disclosed human override of a failed
  pre-registered clause. The numbers themselves are accurate; the gate
  semantics are dual-registered.
- Frame-qualified, not universal: the 9.3% unsafe figure is prod_v3-specific;
  tight-frame unsafe is 0.3333 and exact-span unsafe 0.3082 (same file).
  Quoting 9.3% without "on production 12h frames" would be cherry-picked;
  the docs consistently frame-qualify it.

### 8.3 91.5% heuristic baseline

Claim (docs/ml.md:132-138): heuristic fallback 91.5% top-1 (439/480,
60/class), confidences ≤0.7, flagged `heuristic=true`.

- HONEST but SYNTHETIC-ONLY — measured on the deliberately separable mini
  generator (`synthetic.py:19-21`: "signatures are deliberately separable";
  ml.md §5 header carries the PRELIMINARY caveat). On production frames the
  same heuristic scores **top-1 0.4402 full / 0.3585 test, macro-F1 0.16–0.22**
  (`exp02_baselines_prod_frames/metrics.json`, baselines.heuristic). The
  91.5% number is fine where it sits (inside the synthetic §5 with its
  caveat) but is STALE as any characterization of cold-start quality on
  real-shaped data — off by ~2.4×.
- Safety-relevant and verified: heuristic unsafe_coverage 0.0 everywhere
  (caps ≤0.7 < 0.85 floor) — the fallback fails safe, confirmed in data.
- UNCERTAIN (minor): no standalone metrics.json for the 91.5% run was found;
  the figure is an ml.md record (claim-matrix 6.3 "Attested" via the record
  + code caps), not re-derivable from experiment files.

### 8.4 DIAGNOSIS_WINDOW_RESCOPE opt-in implication

- The knob exists (`rescope.py:53-91`, env `DIAGNOSIS_WINDOW_RESCOPE` or
  constructor arg), defaults OFF (`service.py:73-76`), and tightens diluted
  12h detection frames to the floor-breaching span before feature
  computation — the exact train/serve skew the exp01–exp07 campaign was
  built to close *in the data*.
- **Not re-anchored** (docs/ml.md:274-278: "unit-tested ... but NOT yet
  re-anchored — readings with the knob on must be re-measured in
  docs/evaluation.md before they are quoted as canonical"). Implication:
  every published diagnosis number describes as-detected (dilated) frames.
  Flipping the knob changes the scored-frame distribution at inference time
  WITHOUT any corresponding re-measured accuracy/calibration/gate numbers —
  the published metrics would silently stop describing serving behavior.
  The code mitigates honestly: both frames are recorded on the prediction
  row and the explanation (service.py:125-131, 214-231), and triage failures
  degrade to the original frame (service.py:151-166).
- Live state: OFF by default; `DIAGNOSIS_WINDOW_RESCOPE` does not appear in
  the baseline's env-var name list (docs/audit/baseline.md:35) — presumed
  OFF in production, UNCERTAIN (Render env not inspectable from here).
- Verdict: the *code* is WORKING and honest; the *metric posture* is
  UNCERTAIN for rescope-on. Any future enablement invalidates the published
  anchors until re-measured.

## 9. Findings summary (severity-tagged)

- **[INFO] Leakage discipline is real, not ceremonial** — temporal
  contiguous splits, time-aware calibration CV, pre-registered selection,
  labels/features provably disjoint (grep-verified), a caught merge bug kept
  on record, sha256-pinned dataset reuse.
- **[INFO] Shipped artifact reproduces exactly** — exp08 4dp re-measure,
  active pointer + rollback incumbent both present; serving falls back to a
  capped, fail-safe heuristic on any artifact problem.
- **[MEDIUM] Ship gate was a disclosed human override** — `metrics_v4b2.json`
  records `gate.all: false, gate_passers: []` under the stricter
  pre-registered span macro-F1 clause (0.7664 < 0.7931); the ship verdict
  follows the older exp06 top-1 reading with the conflict disclosed in
  prose. Numbers honest; gate governance dual-registered — a Principal
  should decide which operationalization is canonical going forward.
- **[MEDIUM] Fresh-data accuracy is materially below the headline blocks** —
  exp08 live probes on unseen small-scale seeds: 10/15 top-1 (66.7%) vs the
  recorded 0.7154 prod-frame top-1; all misses hedged ≤0.6028 (fail-safe
  side), every ≥0.80 answer correct. True precision at the auto-execute
  floor looks fine; raw top-1 does not generalize to small scales.
- **[MEDIUM] auto_coverage collapsed to 0.364 on prod frames** — the price
  of cutting unsafe 0.567 → 0.093: most genuinely recoverable incidents now
  hedge into the human-approval lane. Disclosed (SHIP_VERDICT.md:107-111);
  it is a throughput cost, not a safety issue.
- **[LOW] "91.5% heuristic" is synthetic-only** — prod-frame heuristic top-1
  is 0.44/0.36 (exp02). Fine where documented; misleading if quoted for
  production-shaped cold start.
- **[LOW] Exact-span weaknesses shipped knowingly** — bank_downtime F1 0.000
  (support 15), span ECE 0.1465; confusions stay in-lane or bias to
  no_fault (MODEL_CARD.md:50-58). Thin-class supports make per-class numbers
  indicative, not precise.
- **[LOW] Episode-adjacency optimism channel** — near-duplicate rows can
  straddle split boundaries (disclosed, bounded, unmitigated at this dataset
  size).
- **[INFO] All diagnosis numbers are simulator-relative** — labels come from
  simulator ground truth; there is no real-traffic validation of the
  diagnosis model anywhere in the repo (the live Razorpay account cannot
  generate it). Generalization to real merchant traffic is UNCERTAIN by
  construction.

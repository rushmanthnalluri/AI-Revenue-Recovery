# exp03 — Failure analysis (prod_frames_v1, 259 rows / 72 seeds)

Selected candidate: `random_forest` (raw), model_version
`v20260827T123306Z-609981e3`, selected on validation by the pre-registered
rule (val safe 0.2941, val macro-F1 0.6125 — both best-of-9). Row-level
material: `row_predictions.csv`, `error_analysis.json` (regenerate with
`ml/experiments/diagnosis/error_analysis.py`).

Scale caveat: val n=51, test n=53 — per-class supports as low as 2; treat
single-row differences as noise. This motivated the v2 scale-up (exp04/05)
BEFORE any deploy decision.

## Per-class confusion (test block, n=53; 21 errors)

- `no_fault` 15/15 correct, `gateway_degradation` 4/4 correct — the two
  highest-stakes directions (absorb FPs; catch the classic outage) are clean.
- `subscription_failure_spike` 0/7: diluted 12h windows of a 48h
  subscription wave are feature-poor (subscription share of a 12h window is
  small; the wave's failures drown in organic noise) → scattered to
  `gateway_degradation` (2), `customer_insufficient_funds_wave` (2),
  `no_fault` (2). Same failure mode the old artifact showed on the eval
  harness (docs/evaluation.md §3) — window dilution, not model confusion.
- `method_outage` 7/12: misses go to `no_fault` (3) — mild severity-jittered
  outages whose method-level delta is small against a diluted baseline; the
  intended-safe direction.
- `abandonment_spike` 0/4: predicted `method_outage` (2), `route_latency`
  (1), `no_fault` (1). Pending-rate features on dense 12h windows confound
  abandonment with organic slow traffic (assumption review §2, exp01).

## Calibration failure modes

- Overconfident-wrong (confidence >= 0.85 and incorrect): **1/53** test rows
  (vs 5+/53 for the raw-LR class of models — see the current-artifact
  baseline's false_fire_rate 0.1351 in exp02). RF vote-fractions naturally
  hedge on ambiguous windows; its ECE 0.1357 halves the current artifact's
  0.2820 on the same block.
- Wrong-side confidence: errors concentrate at mid confidence (quantiles in
  `error_analysis.json`), i.e. the model is unsure exactly where it should
  be — the calibration-desirable pattern.
- The cost of that hedging: auto-recoverable coverage drops (test
  auto_coverage 0.25 vs the current artifact's 0.6875) — see the deploy
  decision in exp05.

## Sigmoid calibration caveat (all three algos)

`*+sigmoid` candidates collapsed to safe=0.0 on BOTH val and test (Platt
scaling on 3 time-aware folds of ~52-104 rows pulls all max-probas under
0.85) and often worse macro-F1. Isotonic avoided the collapse but did not
beat raw RF on the validation business metric. On this frame size, raw RF's
intrinsic vote-fraction calibration is already the best measured option;
CalibratedClassifierCV needs more data to help — revisited in exp05 on v2.

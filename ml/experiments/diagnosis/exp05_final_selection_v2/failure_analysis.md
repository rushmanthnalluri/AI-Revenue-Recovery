# exp05 — Failure analysis (prod_frames_v2, 506 rows / 144 seeds; DEPLOYED candidate)

Selected + deployed: `random_forest` (raw), model_version
`v20260827T125532Z-b20a153f`. Held-out test n=102, val n=101. Row-level
material: `training/row_predictions.csv`, `training/error_analysis.json`
(regenerate: `ml/experiments/diagnosis/error_analysis.py`). Test block was
touched once, by this candidate (and the baselines it was compared against),
after validation selection.

## Per-class confusion (test block, n=102; 24 errors)

- `no_fault` **31/31 correct**, `gateway_degradation` **7/7** — the two
  directions that matter most for safe automation (absorb detection FPs;
  catch the fleet-wide outage) are error-free on this block.
- `method_outage` 15/22: all 7 misses go to `no_fault` — mild
  severity-jittered outages diluted in a 12h window; fails toward the safe
  side, never toward a confident wrong cause.
- `subscription_failure_spike` 7/12 (P=1.000): misses → `no_fault` (3),
  `customer_insufficient_funds_wave` (1), `gateway_degradation` (1). The 48h
  wave diluted into 12h windows remains the hardest real frame; much improved
  vs v1 (0/7) because v2 carries 45 sub-spike rows (vs 23).
- `customer_insufficient_funds_wave` 15/20 (P=0.882): 5 misses, 2 to
  `no_fault`, 2 to `method_outage` (both are UPI/card failure waves — the
  method-concentration features genuinely overlap; documented in the exp01
  assumption review §4).
- `abandonment_spike` 0/4 (support 4 — indicative only): each miss a
  different class; the pending-rate confound on dense windows (assumption
  review §2) persists. `route_latency` 3/5; `bank_downtime` 0/1.

## Diluted-window failure modes

- Error rate by scenario (test): payday 0.04 (1/26), standard 0.38, storm
  0.25, upi_outage 0.32 — payday waves are long/strong and easy; standard's
  short mild incidents are hardest.
- Error rate by detection metric: success_rate 0.25 vs latency 0.22 —
  frame is not the discriminating factor; severity is.
- Multi-incident-overlap rows (29/102): error rate 0.414 vs 0.164 on
  single-incident rows — the single-cause feature contract's documented
  ceiling (storm concurrency; largest-overlap labeling).
- Median confidence: correct 0.667, wrong 0.647 — errors are hedged, not
  confident; the 90th percentile of wrong-row confidence (0.780) stays under
  the 0.85 auto-execute floor.

## Calibration failure modes (overconfident-wrong)

Only **2/102** test rows cross 0.85 while wrong (val: 0/101):
1. `abandonment_spike` → `method_outage` @ 0.930 (standard) — dense-window
   pending confound; would have entered the auto lane (predicted class is
   auto-recoverable): the residual false-fire (test false_fire_rate 0.0139).
2. `subscription_failure_spike` → `customer_insufficient_funds_wave` @ 0.897
   (storm) — predicted class is NOT auto-recoverable, so the strategy layer
   never auto-executes off it (action_fit(insufficient) <= 0.55); harmless
   for automation, wrong for routing.

Compare the previous artifact on the same block: 18.1% of non-auto incidents
false-fired at >=0.85 (13/72), unsafe_coverage 0.528. The deployed model's
unsafe side is 0.111 — the calibration improvement is the ship decision.

## Known rejections (validation, pre-registered)

- `*+sigmoid` collapsed (safe 0.0 everywhere): Platt scaling on 3 time-aware
  folds of ~100 rows pulls every max-proba under 0.85 — degenerate at this
  frame size.
- `random_forest+isotonic` nearly tied raw RF on test safe (0.1917 vs
  0.1889) with higher auto coverage (0.567 vs 0.30) but worse unsafe (0.375
  vs 0.111); it lost clearly on VALIDATION (0.0436 vs 0.3342) and the rule
  is the rule — noted as the first candidate to revisit at larger frame
  scale, not re-selected on test.

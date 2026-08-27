# exp06 — SHIP VERDICT: NO-SHIP (old artifact kept), with the full constraint map

Verdict date: 2026-08-27. Active pointer after this experiment: the OLD
artifact `logistic_regression v20260826T234303Z-c5434878` (restored; the
exp05 promotion was rolled back by re-pointing, the exact inverse of how it
was applied).

## The hard constraints a deployable artifact must satisfy simultaneously

1. **Prod-frame deploy gate** (pre-registered, exp05): on the prod_frames_v2
   test block (n=102) vs the old artifact — safe_auto_lane_coverage strictly
   better, unsafe_coverage not worse, macro-F1 not materially worse.
2. **Demo operational checks** (suite must be green; demo_run.py is not the
   diagnosis track's file to edit):
   - Scenario A (mild 2.5h fail_boost 0.12, 240-min window, 70k events/day):
     the auto-lane pick is a TIMEOUT payment (fit 0.98) → diagnosis
     confidence >= 0.867 required. Scenario config docstring records it was
     tuned to the old model's measured 0.8997.
   - Scenario D (strong 1.5h fail_boost 0.35, 180-min window, 30k/day): the
     auto-lane pick is a SOFT_DECLINE payment (fit 0.90) → diagnosis
     confidence >= 0.944 required.

## Every candidate, measured on both demo frames + the prod gate

(candidate_frames.json; demo confidences re-measured for this table)

| candidate | demoA conf (>=0.867) | demoD conf (>=0.944) | prod gate | verdict |
|---|---|---|---|---|
| logistic_regression | 0.562 wrong label ✗ | 0.983 ✓ | PASS | fails A |
| LR+sigmoid | 0.356 ✗ | 0.648 ✗ | fail | — |
| LR+isotonic | 0.478 ✗ | 0.896 ✗ | fail | — |
| random_forest | 0.547 ✗ | 0.968 ✓ | PASS | fails A |
| RF+sigmoid | 0.838 ✗ | 0.879 ✗ | PASS | fails A |
| **RF+isotonic** | **0.941 ✓** | **0.910 ✗ (misses by 0.034)** | **PASS (best prod safe 0.2194)** | fails D, barely |
| gradient_boosting | 0.977 ✓ | 0.998 ✓ | **fail: unsafe 0.7083 > 0.5278** | fails gate |
| GB+sigmoid | 0.714 ✗ | 0.764 ✗ | PASS | — |
| GB+isotonic (val winner) | 0.853 ✗ | 0.806 ✗ | PASS | fails both |

No candidate satisfies all constraints. The exp05 v2 RF (prod-frame
champion: safe 0.1889, unsafe 0.111) fails scenario D outright (0.239).

## Why keeping the old artifact is the right call here

- The mission's own gate says: deploy only on evidence of improvement
  without unsafe regression; otherwise KEEP THE OLD ONE and say why. The
  "why": every measured improvement on the production frame is coupled to a
  regression on the demo's tight ad-hoc frames — a second production frame
  family the exp05 dataset did not cover.
- The demo constraints are not arbitrary: scenario A's config was tuned to
  the old model's operating point. Retuning the demo (or the 0.85 policy
  floor) is a product decision for the lead, not a diagnosis-track edit.
- What the old artifact costs is measured, not hidden: on production frames
  it crosses the auto-execute floor on 52.8% of non-auto-recoverable
  incidents and false-fires 18.1% (exp02/exp05). That risk is now on the
  record with exact numbers.

## Recommended follow-up (scoped, not started — out of session budget)

exp07 — ad-hoc-frame augmentation: add demo-style tight detection windows
(180/240-min, success_rate, anchored inc_end+25min) across the density
cycle EXTENDED to 70k events/day (demo A's density — both v2 and v3 top out
at 30k/day, a measured OOD driver of the demo-A hedging), labeled by the
same prodframe rule. RF+isotonic missed scenario D by 0.034 of confidence;
the gap is plausibly closable with density-matched tight frames. Until such
evidence exists, the old artifact stays.

## Artifacts left behind (not active)

- exp05: `diagnosis_random_forest_v20260827T125532Z-b20a153f.joblib`
- exp06: `diagnosis_gradient_boosting+isotonic_v20260827T160357Z-1781945e.joblib`,
  `diagnosis_random_forest+isotonic_v20260827T162752Z-1781945e.joblib`
  (the closest candidate: passes the prod gate and demo A, misses demo D
  by 0.034; also beats the old artifact on its own exact-span frame —
  top-1 0.9049 vs 0.8780, ECE 0.0618 vs 0.0510)
All remain in `backend/artifacts/` and their experiment dirs for the lead's
review; the pointer is what controls serving, and it points at the old
artifact.

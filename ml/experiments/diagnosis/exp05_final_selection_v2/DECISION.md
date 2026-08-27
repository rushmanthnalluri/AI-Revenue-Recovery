# exp05 — Deploy / keep decision

> **SUPERSEDED 2026-08-27 (same day):** the deployment below was ROLLED BACK
> after the full suite caught a tight-frame regression this gate did not
> anticipate — the v2 model is trained only on 12h scheduled windows and
> hedges on the demo's 180-min ad-hoc window (gateway_degradation 0.239 vs
> the old artifact's 1.000, measured), breaking demo scenario D. Full story
> and final verdict: `../exp06_dual_frame_v3/DECISION_log.md` and
> `../exp06_dual_frame_v3/SHIP_VERDICT.md` (NO-SHIP; old artifact active).
> This file is kept unedited as the record of the decision as made.


## The gate (pre-registered before the v2 test block was evaluated)

Deploy the v2 winner iff, on the v2 HELD-OUT TEST block:

1. `safe_auto_lane_coverage(new) > safe_auto_lane_coverage(current artifact)`
   — evidence of improvement on the production-frame business metric;
2. `unsafe_coverage(new) <= unsafe_coverage(current)` — no unsafe-calibration
   regression;
3. `macro_f1(new) >= macro_f1(current) - 0.03` — no accuracy collapse from
   hedging.

Otherwise keep the old artifact and document why.

## Measurement (same test block, n=102; current artifact loaded by filename)

| metric | current LR `v20260826T234303Z-c5434878` | v2 winner RF `v20260827T125532Z-b20a153f` | delta |
|---|---:|---:|---:|
| macro-F1 | 0.4375 | 0.5793 | **+0.142** |
| top-1 | 0.5686 | 0.7647 | +0.196 |
| top-3 | 0.7549 | 0.9314 | +0.177 |
| macro-FPR | 0.0618 | 0.0398 | −0.022 |
| ECE | 0.2683 | 0.2007 | **−0.068** |
| Brier | 0.7480 | 0.4326 | −0.315 |
| **safe auto-lane coverage** | 0.0055 | 0.1889 | **+0.183** |
| auto_coverage | 0.5333 | 0.3000 | −0.233 |
| unsafe_coverage | 0.5278 | 0.1111 | **−0.417** |
| false_fire_rate | 0.1806 | 0.0139 | **−0.167** |

Validation agrees in sign on every business component (safe 0.334 vs 0.181;
unsafe 0.029 vs 0.485).

Gate result: (1) +0.183 > 0 PASS · (2) 0.111 <= 0.528 PASS · (3) 0.579 >=
0.408 PASS → **DEPLOY**.

## What shipped

`diagnosis_random_forest_v20260827T125532Z-b20a153f.joblib` promoted to
`backend/artifacts/` by copying the exact evaluated bytes + pointer (the
artifact the numbers above were measured on — not a retrain).
`backend/artifacts/diagnosis_active.json` updated accordingly.

## The honest trade (disclosed, not hidden)

The old artifact covered more auto-recoverable incidents above the floor
(0.53 vs 0.30) — but it paid for that coverage by crossing the floor on
**52.8%** of NON-auto-recoverable incidents, false-firing 18.1% of them into
auto-executable retry/route-around classes. Under the mission's objective
(safe recovery, false positives priced), that is the unsafe side dominating:
net safe coverage 0.006 ≈ no better than chance. The new model buys a +0.18
net by collapsing the unsafe side 4.7x at the cost of 0.23 coverage — most
borderline incidents now take the human-approval lane, which is exactly the
designed behavior for uncertain frames.

## What was rejected and why

- All 3 `+sigmoid` candidates: degenerate safe=0.0 (see failure_analysis.md).
- LR/GB raw + all isotonic variants: lost the validation rule (safe first).
- v1-scale conclusions (exp03): not acted on — val/test flip of the business
  ranking at n≈52 showed the frame was too small to decide; resolved by
  scaling to v2 BEFORE the decision, not by picking a convenient block.

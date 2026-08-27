# exp06 — Decision log: the v2 tight-frame regression and the dual-frame fix

Chronology (all times 2026-08-27 UTC, every number from the recorded runs):

1. **exp05 deployed** `random_forest v20260827T125532Z-b20a153f` (trained on
   prod_frames_v2 — 12h scheduled detection windows only). The
   pre-registered gate passed on the v2 test block (n=102): safe 0.1889 vs
   0.0055, unsafe 0.111 vs 0.528, macro-F1 0.5793 vs 0.4375.
2. **Full suite caught a regression the gate did not anticipate:**
   `tests/demo` scenario D ("gateway timeout -> UNKNOWN") failed with the v2
   artifact, passed with the old one (A/B verified by re-running scenario D
   under each pointer). Root cause measured directly on the demo's 180-min
   ad-hoc detection window (seed 13, 2d/60k, 1.5h fail_boost 0.35):
   - old artifact: `gateway_degradation` confidence **1.000**
   - v2 artifact: `gateway_degradation` **0.239** (top class, hedged)
   Strategy confidence = diagnosis x action_fit (0.98) therefore fell below
   the 0.85 auto-execute floor; the execute call took the approval lane
   (PENDING_APPROVAL) instead of executing into the injected outage.
3. **Diagnosis of the diagnosis failure:** v2 contains ONLY 12h diluted
   windows. Production serves two frame families — scheduled 12h passes AND
   ad-hoc/tight windows (the demo's 180-min window, API investigations).
   exp05 closed the exact-span→12h skew but created a 12h→tight-window
   blind spot. A frame-coverage bug, not a calibration bug.
4. **Response:** `dual_frame_v3` = prod_frames_v2 (506) + sim_features.csv
   (2050; exact spans + randomized tight/wide frames) = 2556 rows, per-source
   temporal 60/20/20 split so exp05's v2 test block and §8's exact-span test
   block remain exactly held out. Same 9 candidates, same seed (42), same
   pre-registered selection rule. Winner: `gradient_boosting+isotonic`
   `v20260827T160357Z-1781945e` (val safe 0.1527 — best of 9).

## Winner on the two held-out frames (test, n=102 / n=410)

| frame | macro-F1 | top-1 | ECE | safe | auto | unsafe | false-fire |
|---|---:|---:|---:|---:|---:|---:|---:|
| production detection windows | 0.5538 | 0.7059 | 0.1605 | 0.0861 | 0.600 | 0.5139 | 0.0139 |
| exact-span frames | 0.7506 | 0.8951 | 0.0826 | 0.3091 | 0.771 | 0.4623 | 0.0000 |

## Deploy gate verdict (exp05's pre-registered clauses, prod-frame test block)

1. safe: 0.0861 > 0.0055 (old artifact) — PASS
2. unsafe: 0.5139 <= 0.5278 — PASS (marginal, disclosed)
3. macro-F1: 0.5538 >= 0.4375 - 0.03 — PASS

## v2 vs v3 — the honest trade

- v2 RF is the better PROD-frame citizen: safe 0.1889 vs 0.0861, unsafe
  0.111 vs 0.514. But it is operationally broken on tight ad-hoc windows
  (demo red, auto lane unreachable there).
- v3 GB+isotonic keeps tight-frame competence (exact-span top-1 0.8951 vs
  v2's 0.6707; demo scenario D operational check below) while still beating
  the old artifact on every prod-frame gate clause.
- Critically, both keep the dangerous path closed: FALSE-FIRE (conf >= 0.85
  AND predicted class auto-recoverable | true class not) is 0.0139 on prod
  frames under both. v3's higher strict unsafe side is confident CORRECT
  classifications of non-auto classes (routing value), not auto-exec risk.

Decision: **ship v3** if it also passes the operational checks (demo suite,
full backend suite) — results recorded in this directory's SHIP_VERDICT.md
once the suites have run. If it fails them: revert to the old artifact and
keep v2/v3 as documented candidates (the exp05 pointer change would be
rolled back the same way it was applied — file copy).

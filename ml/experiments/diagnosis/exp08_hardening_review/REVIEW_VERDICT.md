# exp08 — HARDENING REVIEW verdict: **KEEP INCUMBENT** `random_forest v20260828T013109Z-77a4ef3b`

Review date: 2026-08-28 (final submission hardening). Reviewer scope:
re-VERIFICATION ONLY — no new datasets, no retraining, no candidate search.
Active pointer unchanged: `backend/artifacts/diagnosis_active.json` →
`diagnosis_random_forest_v20260828T013109Z-77a4ef3b.joblib`.
Rollback incumbent LR `v20260826T234303Z-c5434878` untouched in
`backend/artifacts/`.

## What was re-verified, with fresh numbers

1. **Dataset integrity.** sha256 of `prod_frames_v3.csv` / `tight_frames_v1.csv`
   re-hashed 2026-08-28 and matched to `exp07_tight_frame_v4/config_v4b2.json`
   before any scoring (the remeasure aborts on drift). Held-out blocks rebuilt
   from those bytes with the same `temporal_split(df, 0.6, 0.2)` call as
   `run_v4.py` — datasets reused, never rebuilt.
2. **Calibration + full metric blocks re-measured** by re-scoring the SHIPPED
   artifact (loaded through the active pointer) on the recorded blocks.
   Every leaf number matches `exp07/ship_metrics.json` at 4dp
   (`block_remeasure.json`, `recorded_vs_fresh_diffs` all empty):
   - prod_v3 test (n=130): macro-F1 0.6293, top-1 0.7154, **ECE 0.1291,
     Brier 0.4590**, safe 0.2708, unsafe 0.0928, auto_coverage 0.3636.
   - tight test (n=87): macro-F1 0.5991, top-1 0.8506, **ECE 0.0524,
     Brier 0.2326**.
   - exact-span test (n=410): macro-F1 0.7664, top-1 0.9098, **ECE 0.1465,
     Brier 0.1907** — the disclosed weaknesses reproduce exactly, no drift.
3. **Pre-registered gate re-run end-to-end.** The exp06 incumbent LR was
   re-scored fresh on the identical blocks (its numbers also match the exp07
   record: prod F1 0.4991 / safe 0.1906 / unsafe 0.567; span F1 0.8231 /
   top-1 0.878 / ECE 0.0510). Every hard clause re-evaluates PASS on fresh
   numbers: safe strictly better (0.2708 > 0.1906), unsafe materially lower
   (0.0928 ≤ 0.567 − 0.10), prod macro-F1 not materially worse (0.6293 ≥
   0.4691 — beats outright), span top-1 continuity (0.9098 ≥ 0.848). Demo A/D
   operating points were NOT re-computed (scope exclusion); the recorded
   0.974 / 1.000 pass stands as recorded and is labeled `_recorded` in
   `block_remeasure.json`. The stricter pre-registered span macro-F1 reading
   still fails (0.7664 < 0.7931) — disclosed in exp07, unchanged, not
   re-litigated here.
4. **No-leakage re-verified at the code level.** `features.py` reads only
   `Payment` + `PaymentEvent` (payload/meta/columns) — grep-verified zero
   ground-truth/simulator references in the module; `features_to_vector`
   consumes only the 58 `FEATURE_NAMES`, so the CSVs' GT-derived audit columns
   (`matched_entity_id`, `overlap_seconds`, `overlapping_entity_ids`,
   `n_gt_spans`) are provably not model inputs (column diff recorded in
   config.json). Labels: every dataset builder (exp01, exp07) goes through
   `load_ground_truth(db, sim.run_id)` → `GroundTruthSpan` →
   `label_detection_window` (prodframe-label-v1) — labels only from
   `simulator_ground_truth`.
5. **Live TestClient check on TWO fresh unseen seeds/scales** (real HTTP
   stack: POST /api/v1/detection/run scheduled passes, then
   GET /api/v1/incidents/{id} auto-diagnosis; ground-truth matching by the
   same prodframe-label-v1 rule, mechanical not eyeballed):
   - seed 888, 4d/36k (9k/day): 15 passes, 4 incidents, top-1 **2/4**.
   - seed 1234, 6d/55k (~9.2k/day): 23 passes, 11 incidents, top-1 **8/11**.
   - Every diagnosis served by `diagnosis-random_forest @
     v20260828T013109Z-77a4ef3b` (the shipped bytes).
   - Combined 10/15. All 5 misses at hedged confidence ≤0.6028 — zero wrong
     answers anywhere near the 0.85 auto-execute floor; every answer ≥0.80
     was correct (0.8147, 0.9442×2, 0.9893×2). Miss shapes: two exact GT
     ties (model picked the other tied span), one 33h merged multi-episode
     span (model picked one of the overlapping episodes), one
     overlap-majority wave vs abandonment, one thin 35-min span → hedged
     `no_fault` (safe direction). This is the failure shape the exp07 model
     card already discloses (thin classes, hedged in-lane/no_fault
     confusions), seen on harder small-scale probes — not a new behavior.

## Why KEEP (and what would have changed the verdict)

- The shipped artifact reproduces its recorded numbers exactly on the
  recorded blocks; the gate that shipped it re-passes with both sides
  re-measured fresh; the live stack classifies fresh unseen incidents with
  no unsafe overconfidence. Nothing regressed, so nothing is replaced.
- A REAL regression would have been: any 4dp drift on the recorded blocks,
  any hard gate clause flipping on fresh numbers, a wrong live diagnosis at
  ≥0.85 confidence, or leakage appearing in the feature path. None occurred.
- The disclosed weaknesses (span macro-F1 0.766 on thin minority classes,
  span ECE 0.146, prod auto_coverage 0.364) stand exactly as recorded at
  ship time — re-confirmed, not improved, and honestly NOT fixed by this
  review (fixing them is a new campaign, out of scope for hardening).

## Files

- `remeasure_blocks.py` + `block_remeasure.json` — integrity-pinned
  re-scoring, fresh-vs-recorded diffs, gate re-evaluation.
- `live_check_rerun.py` + `live_check_seed888.json` + `live_check_seed1234.json`
  — fresh live runs (seeds/scales disjoint from every training seed and from
  exp07's seed-777 probe).
- `config.json` — scope, commands, unseen-seed contract.
- Tests: `tests/diagnosis tests/agent tests/agenteval` — 102 passed
  (2026-08-28, this review).

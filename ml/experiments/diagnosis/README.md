# Diagnosis track — experiment index (2026-08-28)

Mission: close the train/serve skew (exact spans -> the diluted detection
windows production serves) and add calibration measurement, through the full
scientific loop. Outcome: **SHIPPED in exp07 —
`random_forest v20260828T013109Z-77a4ef3b` is active** (the exp06 incumbent
`logistic_regression v20260826T234303Z-c5434878` stays committed for
rollback). exp01–exp06 were the NO-SHIP arc that built the production-frame
datasets, the calibration/business-metric machinery, and the constraint map.

Chain (each dir has config.json + metrics/results + analysis):

1. `exp01_prod_frames_dataset/` — leakage audit, class-imbalance analysis,
   58-feature assumption review; builder for the production-frame dataset
   (one row per persisted detection incident, real unmodified detection
   engine, 72 seeds; `prodframe-label-v1`). `leakage_audit.md` is the audit.
2. `exp02_baselines_prod_frames/` — current heuristic + current artifact
   scored on the new frames BEFORE any candidate.
3. `exp03_calibration_candidates/` — 9 candidates (LR/RF/GB x raw/sigmoid/
   isotonic, time-aware calibration CV) on v1 (259 rows): RF won validation,
   but n≈52 blocks could not resolve the business metric -> scale up first.
4. `exp04_prod_frames_v2/` — second 72-seed shard + merge (506 rows);
   `CONTAMINATION.md` discloses the mid-build detection-code change.
5. `exp05_final_selection_v2/` — baselines + 9 candidates on v2: RF raw won
   (test safe 0.1889 vs old 0.0055, unsafe 0.111 vs 0.528) and was deployed —
   then rolled back hours later when the demo suite caught the tight-frame
   regression (see 6). DECISION.md carries the superseded banner.
6. `exp06_dual_frame_v3/` — dual-frame v3 (2556 rows), all candidates scored
   on prod gate + exact spans + both demo operating points; no candidate
   satisfies every hard constraint -> NO-SHIP, old artifact restored.
   `SHIP_VERDICT.md` is the constraint map and the exp07 recommendation.
7. `exp07_tight_frame_v4/` — the exp06 follow-ups executed: prod_frames_v3
   (rebuilt on the stabilized detection engine, density to 70k/day),
   tight_frames_v1 (the missing ad-hoc frame family), then a measured
   iteration 2 (aug_pure_sr_v1, train-only) closing the demo-A OOD hole
   (latency_multiplier=1.0 pure success-rate drops). **SHIP
   `random_forest v20260828T013109Z-77a4ef3b`** — passes every gate clause
   (prod safe 0.2708 vs 0.1906, unsafe 0.0928 vs 0.567, demo A 0.974,
   demo D 1.000, span top-1 0.9098 vs 0.8780). `SHIP_VERDICT.md` is the
   gate account incl. the disclosed macro-F1 tradeoff and the v4b
   merge-policy bug (caught, corrected, kept on record).

Shared tools: `error_analysis.py` (row-level failure cuts),
`live_check.py` (TestClient end-to-end diagnosis check; `live_check.log`).

Datasets (in `backend/artifacts/`, gitignored): `prod_frames_v1.csv`,
`prod_frames_v2_shard2.csv`, `prod_frames_v2.csv`, `prod_frames_v3.csv`,
`tight_frames_v1.csv`, `aug_pure_sr_v1.csv`. Training CLI:
`backend/scripts/train_models.py --input <csv> --experiment-dir <dir>`.

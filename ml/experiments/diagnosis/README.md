# Diagnosis track — experiment index (2026-08-27)

Mission: close the train/serve skew (exact spans -> the diluted detection
windows production serves) and add calibration measurement, through the full
scientific loop. Outcome: **NO-SHIP — old artifact
(`logistic_regression v20260826T234303Z-c5434878`) remains active**; the
production-frame dataset, the calibration/business-metric machinery, and the
constraint map for the next attempt are the deliverables.

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

Shared tools: `error_analysis.py` (row-level failure cuts),
`live_check.py` (TestClient end-to-end diagnosis check; `live_check.log`).

Datasets (in `backend/artifacts/`, gitignored): `prod_frames_v1.csv`,
`prod_frames_v2_shard2.csv`, `prod_frames_v2.csv`. Training CLI:
`backend/scripts/train_models.py --input <csv> --experiment-dir <dir>`.

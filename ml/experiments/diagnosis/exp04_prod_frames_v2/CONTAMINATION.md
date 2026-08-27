# exp04 — prod_frames_v2 (144 seeds) + contamination disclosure

`prod_frames_v2` = `prod_frames_v1` (exp01; seeds 5000–5071, 259 rows, built
2026-08-27 ~12:05–12:29 UTC) + `prod_frames_v2_shard2` (seeds 5072–5143, 247
rows, built ~12:31–12:51 UTC), merged by `merge_v2.py` → 506 rows
(`backend/artifacts/prod_frames_v2.csv`, sha256 in `dataset_summary.json`).

Why v2 exists: v1's held-out blocks (val 51 / test 53) flipped the val→test
business-metric ranking — statistically unresolvable. The scale-up was
decided from v1's validation behavior BEFORE any v2 test block was seen;
candidates and the selection rule were unchanged.

## Contamination disclosure (found during exp06, documented honestly)

The detection track was concurrently editing `app/services/detection/`
(their new metrics `checkout_abandonment_rate` and `insufficient_fund_share`
land in `KNOWN_METRICS`, so detection passes run them once present).
`series.py`'s mtime (12:45 UTC) falls INSIDE the shard2 build window
(12:31–12:51 UTC): seeds processed after ~12:45 (roughly seeds >= 5122,
~20 of 72 shard seeds, ~70 rows ≈ 14% of v2) were collected with the newer
detection code and may include incidents of the two new metrics; earlier
seeds used only `payment_success_rate` + `capture_latency_ms`.

Materiality: the labeling rule is metric-agnostic and the extra metrics only
ADD candidate incidents of kinds the taxonomy already covers
(abandonment/insufficient-funds classes exist in both shards' label
distributions at similar rates — shard1 no_fault 36.7% vs shard2 38.9%), so
the measured effect is a slight frame-mix heterogeneity rather than a label
error. It is disclosed because dataset provenance must be exact: v2 is NOT a
single-code-version dataset. The exp07 rebuild (recommended in
exp06/SHIP_VERDICT.md) should run against the stabilized detection engine.

Files: `config.json` + `dataset_summary_shard2.json` (shard2 record,
recomputed from the CSV after a since-fixed relative-path crash in the
record writer; per-seed stats in `build_shard2.log`), `merge_v2.py`,
`merge_config.json`, `dataset_summary.json` (the merged v2 summary used by
all downstream experiments).

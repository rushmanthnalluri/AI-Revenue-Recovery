# Detection-recall experiments (2026-08-27)

Track: detection recall. Goal: close the three measured detection blind spots
(route_latency, checkout_abandonment_spike, customer_insufficient_funds_wave)
without giving back the floors+dedup precision win.

## How to reproduce

All measurements are on simulator datasets replayed through the REAL engine
(`run_detection`) with the exact evaluation-harness schedule (12h lookback,
6h step, production defaults):

```
# per-kind replay + scoring (also validates against the official harness)
python ml/experiments/detection/_lib/analyze_detection.py \
    --scenario standard --seed 42 \
    --json ml/experiments/detection/_lib/replay_after_c2_standard42.json

# official end-to-end harness (baseline vs PulseRecover arms)
cd backend && .venv/Scripts/python scripts/run_evaluation.py \
    --scenario standard --seed 42 --name <run-name>
```

`analyze_detection.py` seeds a scratch SQLite DB with the real simulator,
replays the harness's detection schedule, and scores incidents against
`simulator_ground_truth` with the harness's own temporal-overlap rule. The
before/after replay outputs referenced by each experiment live in `_lib/`.

## Experiments

| exp | signal | result |
|---|---|---|
| exp000_baseline | — | pre-change reference: P 0.667 / R 0.500 / F1 0.571 / MTTD 895 (standard/seed42) |
| exp001_route_latency_scan | per-route 15-min latency slices + within-method corroboration | SHIPPED: route_latency 1/1, 0 scan FPs on standard-42/quiet-42 |
| exp002_checkout_abandonment_metric | attempt-based stuck-in-`created` rate, censoring-aware | SHIPPED: spike 1/1, 0 FPs anywhere measured |
| exp003_insufficient_fund_share_metric | IF share of failures, 60-min, near-single-class admission | SHIPPED with documented boundary: wave 1/1 on standard, 0/1 on payday scale (no FPs) |

Final (official harness run `run_0022000d8df942e6ac4b7299986f994a`,
standard/seed42, dataset sim_42_50f24b57d0):
**P 0.778 / R 1.000 / F1 0.875 / MTTD 585 min**, 9 incidents (7 TP rows + the
2 pre-existing organic SR FPs), all 6 incident kinds detected.

Tuning iterations rejected on measurement: v1 unguarded (P 0.348),
v2 min_observed 0.6 (P 0.636, under the 0.65 gate), v3 4-bucket baseline +
persistence (lost the wave, R 0.833). See each exp's failure_analysis.md.

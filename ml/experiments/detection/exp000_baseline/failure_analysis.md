# exp000 failure analysis — the three measured blind spots (pre-change)

Dataset: standard / seed 42 / sim_42_50f24b57d0 (anchored 2026-08-27).
Evidence: `ml/experiments/detection/_lib/probe_before.json` (replay of the exact
harness schedule + per-GT probes; reproduces the harness P/R/F1/MTTD row for row).

## Overall (before)

116 passes, 6 incidents persisted, 4 matched rows, 3/6 ground truth,
P 0.6667 / R 0.5000 / F1 0.5714 / MTTD 895.0 min (sim time).
False positives: 2 organic success-rate dips (08-15 −54.2%, 08-22 −46.65%) —
documented residual FPs from the floors+dedup change, not addressed here.

## Blind spot 1: route_latency (2026-08-13 03:30–05:30 UTC, INR 51,727 affected)

**Measured mechanism — NOT "barely moves merchant-wide latency".** Merchant-wide
mean capture latency per 5-min bucket actually jumps ~6–10x during the incident
(17k–125k ms vs ~2k–14k ms around it; probe `route_latency` table). The z-score
stays silent in BOTH overlapping passes for a different reason:

- The incident window (03:30–05:30 UTC) sits in a sparse traffic band: most
  5-min buckets there carry 1–5 captures, at/under `min_bucket_count=5`.
- In the as_of=09:00 pass (window 21:00–09:00) the leading baseline is built
  from scattered night buckets with huge sample-to-sample spread, so baseline
  sigma is inflated; in the as_of=15:00 pass (window 03:00–15:00) the first 8
  *valid* buckets (count>=5) fall INSIDE the incident itself — classic leading-
  baseline poisoning — and the poisoned sigma suppresses the z-score for the
  rest of the window.
- The docs/detection.md note "a single route barely moves merchant-wide
  latency" is therefore imprecise for this dataset: the aggregate moves, but
  the chart never fires because of sparse buckets + baseline poisoning.

**Fix direction (Exp A):** per-segment (route) latency series — the segment
machinery exists (`payment_segments`/`slice_outcomes`) but `route` was never
extracted from `Payment.meta`. Slice detection must also survive night
sparsity (coarser slice buckets / own count floor) and is only needed when the
merchant-wide pass admitted nothing (avoids duplicate incidents on fleet-wide
latency incidents such as gateway_degradation, where the aggregate DOES fire:
dev +515.79%/+313.7% in the two overlapping passes, both admitted).

## Blind spot 2: checkout_abandonment_spike (2026-08-21 12:30–15:00 UTC, INR 69,201 affected)

Blind by construction as documented: abandoned checkouts stay `created`,
never produce terminal events, so no outcome-based series sees them. Probe
(`abandonment` table, 30-min inactivity threshold): baseline buckets resolve
fully (stuck share 0.0, rare 0.14–0.33 blips on 3–7 creations); during the
spike the stuck share runs 0.25–0.83 for 2.5h on 2–13 creations per 5-min
bucket. Clean separation → feasible with its own floors (isolated low-count
baseline blips die on persistence + volume).

Right-censoring rule adopted: a payment is decidable only when
`created + inactivity_threshold <= window_end` (the pass's knowledge horizon);
unresolved-within-threshold = abandoned; censored payments are excluded from
both numerator and denominator, so the last `threshold` minutes of each window
carry proportionally less (or no) signal instead of being misread as
abandoned.

## Blind spot 3: customer_insufficient_funds_wave (2026-08-16 18:30 → 08-17 14:30 UTC, INR 104,458 affected)

ALL FIVE overlapping passes are silent on both metrics (probe
`detection_outcome`): night buckets carry 1–5 events (under
`min_bucket_count`), and every 12h pass except the straddling one
(as_of 08-17 00:00, baseline 12:00–18:00 UTC healthy) starts inside the 20h
wave → baseline poisoning.

Probe (`insufficient_fund_share`, 30-min buckets): baseline insufficient_fund
share of failures ≈ 0.27 (0.0–0.6 on 2–8 failures); during the wave's morning
band it runs 0.4–0.88 on 4–13 failures — but the straddling pass only sees
the SPARSE night band (00:00–05:30 IST, 0–3 failures/30-min bucket). The
signal therefore needs: coarse buckets (60 min) to make night shares
decidable, its own low count floor (>=2 failures), and its own volume floor
(the global min_flagged_volume=15 events can never be met at night).
Binomial-noise math on the straddling pass: 60-min baseline shares
~0.27 ± ~0.10 → the 22:00/23:00 UTC buckets (share 1.0 / 0.67) exceed z=3
and form the required 2-bucket run.

## What is NOT addressed here

- The two residual organic-dip false positives (same-time-yesterday baselines
  are the real fix — out of scope per docs/detection.md known limitations).
- Recovery of abandoned checkouts (OpportunityBuilder only builds
  failed-payment retries + payment-less dropped orders; abandoned checkouts
  have a payment row in `created`, so they yield no opportunities today —
  downstream scope, reported to lead).

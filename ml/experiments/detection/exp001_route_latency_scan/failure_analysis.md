# exp001 failure analysis — per-route latency scan

## What shipped

Route added to `payment_segments` / `SEGMENT_DIMENSIONS` (it was in
`Payment.meta` all along but never extracted — measured in exp000: slice
queries returned zero rows). When a pass admits no merchant-wide
`capture_latency_ms` incident for a detector, that detector re-scores
per-route slice series (15-min buckets, count floor 3, same z>=3 and standard
incident floors), plus a within-method corroboration guard; admitted slice
anomalies persist with `segment={"route": ...}` and `meta.segment_scan=true`.

## Root cause corrected (supersedes the old doc note)

exp000 measured that the merchant-wide mean capture latency DOES jump ~6-10x
during the injected route_latency (17k-125k ms per 5-min bucket vs ~2k-14k
around it) — the old note "a single route barely moves merchant-wide latency"
is wrong for this dataset. The z-score stays silent because (a) 03:30-05:30Z
buckets carry 1-5 captures, and (b) in the pass whose window starts 03:00Z
the first 8 valid buckets fall inside the incident (leading-baseline
poisoning); the poisoned sigma suppresses the rest of the window.

## Measured slice behavior (lab DB, sim_42_50f24b57d0)

- Window [2026-08-12 21:00Z → 08-13 09:00Z], pg_primary slice, 15-min
  buckets, count floor 3: fires dev +1009%, start 03:30Z (exact injected
  start), 9 flagged buckets → admitted; MTTD 330 min (pass end − start).
- pg_secondary blips (1-2 flagged buckets, dev 160-390%) die on the
  persistence/volume floors in both overlapping windows.
- Poisoned window [03:00Z → 15:00Z]: slice silent too (same disease) — the
  earlier pass detects the incident; accepted.

## Mix-shift FPs and the corroboration guard (v1 → v2)

v1 (no guard): 4 organic scan FPs on standard-42 (dev 129-654%, spans 3-11h).
Payment-level analysis: in every organic case at most ONE method's latency
rose (card 2.4-5.8x) while the others stayed flat — method-mix shifting
inside the route, not a route degradation. The injected incident rises >= 7x
in EVERY method (8x multiplier). Guard: >= 2 methods with >= 3 samples in
both regions must each rise >= 2x (a lone well-sampled method must rise
>= 3x). v2: 0 scan FPs on standard-42 and quiet-42; 1 corroborated organic
latency episode admitted on standard-7 (dev +907%, 07-31 10:00-11:00Z) — a
real organic latency incident, reported honestly as an FP against injected
ground truth.

## Remaining blind spots

- A route latency incident confined to a route whose slices never reach
  count floor 3 on 15-min buckets (very quiet merchants) stays invisible —
  same philosophy as min_bucket_count: no detection rather than noisy
  detection.
- Scan covers the `route` dimension only (the measured gap). Per-method or
  per-bank latency scans are the same machinery but were not needed for the
  measured blind spot; adding them multiplies the FP surface and was not
  done.
- Baseline poisoning is inherited: slices whose leading baseline lands
  inside the incident stay silent; the scan relies on the pass that
  straddles the incident start.

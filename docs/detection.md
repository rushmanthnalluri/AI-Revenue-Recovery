# Detection Engine

Owner: detection agent. Code: `backend/app/services/detection/`, router
`backend/app/api/v1/detection.py`, tests `backend/tests/detection/`.

The detection engine watches the normalized `payment_events` stream and turns
"the success rate fell off a cliff at 14:05" into a persisted, evidence-backed
`Incident` for the diagnosis / investigation / recovery stages downstream.

> Probabilistic AI proposes. Deterministic policy decides. Detection is the
> *proposal* stage: it never triggers an action, it only opens incidents with
> the numbers a human or agent needs.

## Signal model

### Outcome resolution

A payment's outcome is its **latest terminal event** in the window
(`captured` / `authorized` = success, `failed` = failure). Razorpay's own
semantics say a `payment.failed` webhook can legitimately be followed by
`payment.captured` for the same payment (late authorization, UPI in-app
retry) — so failures are *not* terminal, and the engine resolves outcomes
per payment, not per event.

### Bucketed series

Events are aggregated into a fixed grid of buckets (default 5 minutes):

| Metric | Value per bucket | Degrades |
|---|---|---|
| `payment_success_rate` | successes / terminal outcomes | **down** |
| `capture_latency_ms` | mean capture latency (event `payload["latency_ms"]`, else created→captured gap) | **up** |

Buckets with fewer than `min_bucket_count` (default 5) events carry no
statistical signal and are skipped — with 2 payments in a bucket, one failure
is a 50pp swing, not an incident.

### Segments

Every payment carries segment dimensions used to *localize* a degradation:

- `method` — `Payment.method` (`upi`, `card`, `netbanking`, ...)
- `bank` — `Payment.meta["bank"]`
- `gateway` — `Payment.meta["gateway"]` (default `"razorpay"`)

A run can be restricted to one slice (`segment: {"method": "upi"}`), and every
detected anomaly is automatically broken down per dimension: the engine
re-builds the series per segment value and ranks contributors by deviation,
so the incident evidence says "UPI via icici fell 67%, card is flat" rather
than just "something dropped".

## Baselines-first rationale

Every detector follows the same contract:

1. The **first `baseline_buckets` valid buckets** (default 8) of the window
   are assumed healthy and define the baseline (mean/std or median).
2. Only buckets **after** the baseline are scored.

Why not learn the baseline from all history? Three reasons:

- **Simplicity under deadline pressure.** The engine runs against a demo-day
  dataset (simulator or test-mode traffic) where the window reliably starts
  healthy. A fixed leading baseline needs no stored state, no decay model,
  and is trivially explainable to judges: "this is what normal looked like an
  hour ago".
- **Robustness to the anomaly itself.** A trailing/rolling baseline gets
  *absorbed* by a slow degradation (the baseline degrades with the signal and
  the z-score never fires). Anchoring the baseline at the window start makes
  slow drifts visible for the whole window.
- **Determinism.** Same data → same window → same baseline → same decision,
  which is what makes re-runs idempotent and the evaluation harness
  reproducible.

The baseline std gets a relative floor (`max(sigma, 1% of mean)`) so a
near-perfect healthy baseline (success rate pinned at 1.0) doesn't make the
detectors hair-trigger on trivial wobble.

The trade-off is explicit: if the window *starts* inside an ongoing outage,
the baseline is poisoned and nothing fires. The run-length guidance is
`window >= 2x baseline_buckets` buckets; the simulator-driven evaluation
always anchors windows before the injected incident starts.

## Detectors

All detectors implement one protocol and live in a registry
(`get_detector(name)` / `all_detectors()` / `"all"` in the API):

```python
class Detector(Protocol):
    name: str
    def detect(self, buckets: list[Bucket], params: DetectorParams) -> Anomaly | None: ...
```

Detection is **direction-aware** (drops for success rate, rises for latency);
improvements are never reported as incidents. Flagged buckets are summarized
as one `Anomaly`: `start_ts` = first bucket with degradation evidence (drives
detection latency), `observed`/`deviation_pct`/`score` = worst bucket.

### 1. `zscore` — rolling-baseline z-score

Flags a bucket when its value lies more than `threshold` (default **3.0**)
baseline standard deviations in the degradation direction. Fastest on sudden
step shifts (fires on the first shifted bucket), weakest on slow drifts
(each bucket individually stays under the cut).

### 2. `ewma` — exponentially weighted moving average control chart

Tracks `S_t = λx_t + (1−λ)S_{t−1}` (λ = 0.3) against the time-varying control
limit `L · σ · sqrt(λ/(2−λ) · (1−(1−λ)^2t))`, L = 3.0. Smooths noise, so it
detects smaller sustained shifts than z-score; the smoothing overhangs a few
buckets after recovery (visible as post-recovery flags in the comparison).

### 3. `cusum` — one-sided cumulative sum chart

Accumulates evidence `S_t = max(0, S_{t−1} + deviation − k)` with allowance
`k = 0.5σ`, flags when `S_t > h = 4σ`. The statistic is **capped at h + k**
so the alarm clears within a bucket once the series recovers (uncapped CUSUM
overhangs for the rest of the window after a long episode — measured: 10
false-positive buckets on the recovery scenario before the cap, 0 after).
Best mean-delay trade-off for small persistent drifts.

### 4. `isolation_forest` — multivariate outlier detection (sklearn)

Features per bucket: `[value, first difference, deviation from trailing-3
mean]` — so level shifts, slope changes and sawtooth oscillation all look
anomalous. Design notes from measurement:

- Fitted in **classic mode** (over the whole window, `contamination=0.25`,
  `random_state=42` for determinism). Novelty mode (fit baseline only)
  degenerates on tiny baselines: with ≤12 training points the training
  samples self-isolate, the offset sinks below genuine outlier scores, and
  the detector fires on *nothing* (verified empirically).
- Three gates align it with degradation semantics: post-baseline buckets
  only, direction vs the baseline median, and magnitude ≥ **2.5σ** so
  healthy-series noise outliers don't fire.

It deliberately flags only the most anomalous (boundary) buckets — high
precision, instant detection, low per-bucket recall by design.

### Sensitivity

One knob for all detectors: `sensitivity` (default 1.0, 0–5) divides the
decision threshold (for IsolationForest it scales contamination instead, and
an explicit `threshold` request field overrides the detector default
entirely). Higher sensitivity fires earlier and on smaller shifts, at the
cost of false positives.

## The run pass — `POST /api/v1/detection/run`

```json
{
  "window_minutes": 240,      // analysis window (default 60)
  "bucket_minutes": 5,        // bucket size (default 5)
  "metrics": ["payment_success_rate"],   // default: both metrics
  "detector": "zscore",       // registry name, or "all"
  "segment": {"method": "upi"},          // optional slice restriction
  "baseline_buckets": 12, "min_bucket_count": 5,
  "sensitivity": 1.0, "threshold": null,
  "as_of": null,              // window anchor; default = latest terminal event
  "dry_run": false,
  // --- incident-level noise floors (see below) ---
  "min_absolute_deviation": null,  // metric units; null = per-metric default
  "min_flagged_volume": 15,        // events across flagged buckets; 0 disables
  "min_flagged_run": 2,            // consecutive flagged buckets; 1 disables
  // --- episode dedup + suppression ---
  "dedup_cooldown_minutes": 360,        // null disables merging
  "suppress_after_resolve_minutes": 720 // null disables suppression
}
```

The window is anchored at the **latest terminal event** (or `as_of`), not
wall-clock now — identical data yields an identical window, which is what
makes re-runs deterministic and idempotent.

## Incident-level noise floors (Watchdog-style)

A detector fire is a *statistical* event; an incident is a *product* event.
Between the two sits an engine-level admission gate (detectors stay pure —
the synthetic-fixture comparison above is unaffected). A fire becomes an
incident only when it clears **all three** floors:

| floor | default | meaning |
|---|---|---|
| `min_absolute_deviation` | per metric: **5pp** success rate, **75 ms** latency (`null` in the request selects the default; `0` disables) | `|observed − baseline|` in metric units. Guards quiet-merchant hair-triggers: with the baseline std floored at 1% of mean, a 2–3pp wobble can exceed 3σ on a near-perfect baseline. |
| `min_flagged_volume` | **15** events | total events across the flagged buckets. A "50% drop" built on 6 payments is not an incident. |
| `min_flagged_run` | **2** consecutive buckets | persistence: the degradation must hold for at least 2 adjacent flagged buckets (≥ 10 min at the default grid). One bad bucket is a blip. Note: `isolation_forest` flags boundary buckets by design — pair it with `min_flagged_run: 1`. |

Fires that fail a floor are counted in the run response as
`anomalies_filtered` (and logged with the failed floor), not persisted.
Every floor is request-configurable, so threshold tuning via `dry_run` can
see exactly what would be admitted.

Measured on the standard-scenario harness (scheduled 12h/6h passes,
production defaults, seed 42 — see "Measured effect" below): the binding
floors on that dataset are **persistence** and **flagged volume**; the
absolute-deviation floor does not bind there (organic noise deviates far
more than 5pp) and exists for the small-sample regime.

## Cross-pass episode dedup + post-resolution suppression

**Idempotent re-runs (unchanged):** the upsert key is `(metric,
detection_method, window_start, window_end, segment_fingerprint)`.
Re-running the same (window, segment, detector) **updates** the existing
incident — latest observed/deviation/severity/impact, fresh evidence
(replaced, not stacked) — and deliberately preserves the original
`detected_at` and leaves `status` untouched (a human's triage is never
clobbered by a re-run).

**Episode merge (new):** scheduled passes overlap (the evaluation harness
runs one pass every 6h with a 12h lookback), so the same underlying episode
used to be re-persisted as a new row per pass window — the incident list
filled with duplicates of one anomaly. Now, when there is no exact-window
match, the engine looks for a **non-terminal** incident with the same
`(metric, detection_method, segment_fingerprint)` whose anomaly span
(`meta.anomaly_start..anomaly_end`, falling back to the analysis window)
overlaps the new anomaly or lies within `dedup_cooldown_minutes` (default
360) of it. The match is **merged into the earliest such incident**:
`detected_at`, window bounds and `status` stay with the first detection
(honest MTTD), observed/severity/impact refresh, the episode span widens
(`min` start / `max` end), and `meta.merge_count` increments. The run
report marks the action `updated` with a `detail` note. `dedup_cooldown_minutes: null`
restores the legacy per-window behavior.

**Suppression (new):** re-detection of a signature that a human already
closed must not reopen it. When no live match exists, if a **terminal**
incident (`RESOLVED` / `CLOSED` / `FALSE_POSITIVE`) with the same signature
was resolved (falling back to `updated_at`) less than
`suppress_after_resolve_minutes` (default 720 = 12h) before the new
anomaly's start, the fire is **suppressed**: nothing is persisted, the run
report carries an entry with `action: "suppressed"` pointing at the
suppressing incident, and `anomalies_filtered` increments. After the window
expires, a genuinely new episode creates a fresh incident. Exact same-window
re-runs still *update* a resolved row in place (status untouched) — that is
idempotent replay, not re-detection. `suppress_after_resolve_minutes: null`
disables the rule.

For every (metric × detector) admitted anomaly the engine persists:

- **`incidents` row** — status `OPEN` (the enum's initial state), `metric`,
  `detection_method`, `baseline_value`, `observed_value`, signed
  `deviation_pct` (negative = drop), `severity` from deviation magnitude
  (≥50% CRITICAL, ≥25% HIGH, ≥10% MEDIUM, else LOW), window bounds,
  `detected_at`, preliminary `affected_payments_count` +
  `revenue_at_risk_paise` (failed/slow payments from `anomaly_start` to
  window end), and `meta` with the segment JSON, detector params,
  `anomaly_start`/`anomaly_end`, score and flagged buckets.
- **`incident_evidence` rows** (collector `agent:detection`):
  - `metric_series` — the full bucketed series snapshot used for the decision;
  - `segment_breakdown` — per-dimension contributor ranking
    (`flagged` = deviates in the degradation direction by ≥ half the global
    deviation).

**Detection latency** is computable from the persisted record:
`detected_at` (when the engine saw it) vs the simulator ground-truth start
(evaluation agent), and `meta.anomaly_start` (estimated start) for the
engine's own estimate. `meta.bucket_minutes` converts either to buckets.

`dry_run: true` computes everything and persists nothing
(`action: "would_create"`), for threshold tuning against live data.

## Measured effect of the floors + dedup (evaluation harness)

Before/after on the **same dataset and seed**: scenario `standard` (30 days,
~69k payment_events, 6 injected incidents), seed 42, scheduled 12h/6h passes
at production defaults, reproduced by
`scripts/run_evaluation.py --scenario standard --seed 42`.
⚠️ The simulator anchors its data window at *today* 00:00 UTC, so absolute
numbers shift with the calendar day of the run; the published 0.185/0.833 in
`docs/evaluation.md` was measured on a dataset anchored 2026-08-26, this
pair on 2026-08-27. The before/after *delta* below is what is reproducible —
same day, same seed, same code except the detection change.

| metric | before | after |
|---|---:|---:|
| incident rows persisted | 90 | **6** |
| matched rows / ground truth | 14 / 3 of 6 | 4 / 3 of 6 |
| **precision** | 0.156 | **0.667** |
| recall | 0.500 | 0.500 (unchanged) |
| F1 | 0.237 | 0.571 |
| MTTD (min, sim time) | 415 | 895 |
| downstream: opportunities / interventions / false interventions | 8,460 / 100 / 12 | 719 / 60 / 6 |

Root causes the change attacks, measured:

1. **Organic noise on quiet traffic.** 76 of the 90 before-rows matched no
   injected incident. Two mechanisms: sparse night buckets (5–10 events)
   where one failure swings the rate 20–50pp, and daily-cycle mix shift —
   windows whose baseline buckets sit in the night trough flag the day ramp
   (and vice versa) with deviations up to −55% / +2000%. The persistence
   floor (`min_flagged_run=2`) is the binding gate (alone: precision 0.62 in
   offline replay), flagged-volume (`min=15`) reinforces it; absolute
   deviation does not bind on this dataset.
2. **Pass-window re-detection.** Consecutive overlapping passes re-persisted
   the same episode as new rows (14 matched rows were really 4 episodes;
   76 noise rows were ~45 episodes). The episode merge collapses them.
   Measured honestly: merge *alone* slightly lowers the row-counted
   precision (0.113) — true-positive re-detection had been inflating the
   numerator — the floors are what raise it; the combination is what ships.
3. **Re-detection after resolution.** Covered by the suppression window
   (no harness effect: the harness never resolves incidents).

Honest costs, also measured:

- **MTTD 415 → 895 min.** The floors delay the first *persisted* detection
  until an episode proves persistence/volume — for the 48h subscription
  spike, four early weak detections (5–14 events, single-bucket runs) are
  correctly filtered and the first admitted row lands a pass later. The
  merge preserves the original `detected_at` for every admitted episode.
- **Recovered revenue in the harness drops ~18%** (₹16.06L → ₹13.19L) with
  interventions 100 → 60. The 84 noise incidents had been generating
  recovery work on organic failures — gross-attribution inflation of exactly
  the kind the evaluation methodology criticizes. Post-fix recovery is
  incident-driven only; false interventions halve (12 → 6); unsafe stays 0.
- The two residual false positives (08-15 and 08-22, multi-hour organic
  −47…−54% success-rate swings overlapping no injected window) are real
  traffic dips no floor kills without also killing real incidents —
  precision 0.667, not 1.0.

Recall note: the three missed injected incidents are coverage gaps, not
floor casualties — `route_latency` (a single route barely moves merchant-wide
latency), `checkout_abandonment_spike` (abandoned checkouts never become
terminal outcomes, so the success-rate series is blind by construction), and
`customer_insufficient_funds_wave` (runs 00:00–20:00 IST; its night buckets
carry 1–5 events and fall under `min_bucket_count`, so the signal never
enters the scored series — verified on the persisted series snapshot).

## Detector comparison — synthetic fixtures (preliminary)

Measured by `backend/tests/detection/test_comparison.py` over 6 tiny labeled
scenarios (40 buckets each, 5-min buckets, binomial noise at 100
events/bucket): sharp SR drop 0.90→0.45, moderate drop 0.90→0.70, gradual
drift 0.90→0.60 over 12 buckets, drop with mid-window recovery, latency
spike 250→1500 ms, and a healthy control.

> ⚠️ **Synthetic-fixture results, not production accuracy.** They exist to
> prove the detectors behave as designed and to give the evaluation harness a
> sanity baseline. Real precision/recall gets measured against
> `simulator_ground_truth` by the evaluation agent.

| detector | precision | recall | MTTD (buckets) | FP buckets |
|---|---|---|---|---|
| zscore | 0.989 | 0.978 | 0.2 | 1 |
| ewma | 0.917 | 0.978 | 0.4 | 8 |
| cusum | 0.967 | 0.989 | 0.2 | 3 |
| isolation_forest | 0.923 | 0.400 | 0.2 | 3 |

Read: **zscore** is the best default (near-perfect on steps, instant);
**cusum** tracks drifts best with the cap keeping precision high; **ewma**
smooths best but overhangs after recovery (7 of its 8 FPs are post-recovery
buckets); **isolation_forest** fires instantly and precisely but only on the
most anomalous boundary buckets — its low recall is by design, and its value
is multivariate shape detection (oscillation, slope) the charts don't see.
All four stay silent on the healthy control.

Per-scenario detail (recorded at the same run):

- sharp drop / moderate drop / latency spike: all detectors, latency 0 buckets.
- gradual drift: zscore/cusum/IF latency 1 bucket, ewma 2.
- drop with recovery: charts flag the whole degraded region; ewma overhangs
  ~7 buckets post-recovery; IF flags the transition only.

## Known limitations (honest list)

- Baseline poisoning: a window that starts inside an ongoing outage detects
  nothing (accepted trade-off of baselines-first). This is what keeps the
  20h insufficient-funds wave invisible from windows anchored inside it.
- Sparse traffic: buckets under `min_bucket_count` are skipped; very quiet
  merchants get no detection rather than noisy detection. Measured: the
  wave's night buckets carry 1–5 events, so its signal never enters the
  scored series at the default floor.
- Seasonality: the leading-window baseline cannot tell a daily-cycle trough
  from a degradation. The noise floors + episode dedup suppress the
  resulting incidents (measured above), but multi-hour organic swings still
  occasionally admit (2 residual FPs in the measured run). A same-time-
  yesterday baseline is the real fix and is deliberately out of scope.
- The noise floors trade detection latency for precision: first persisted
  detection waits for persistence + volume evidence (MTTD 415 → 895 min on
  the standard harness). `min_flagged_run=1` / `min_flagged_volume=0`
  restore the old hair-trigger.
- IsolationForest needs ≥ 8 baseline buckets and adds no value below ~20
  total buckets; its boundary-only flagging also pairs poorly with
  `min_flagged_run=2` (use `min_flagged_run=1` when running it).
- Severity is a function of deviation magnitude only; business weighting
  (revenue mix per segment) is the downstream risk agent's job.
- `incident_evidence.evidence_type` adds `segment_breakdown` alongside the
  model comment's example list (`metric_series` was already listed) — the
  column is a free varchar, no migration needed.

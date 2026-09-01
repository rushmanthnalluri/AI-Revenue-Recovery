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

Events are aggregated into a fixed grid of buckets (default 5 minutes; the
two sparse-signal metrics use coarser per-metric grids, see below):

| Metric | Value per bucket | Grid | Degrades |
|---|---|---|---|
| `payment_success_rate` | successes / terminal outcomes | 5 min | **down** |
| `capture_latency_ms` | mean capture latency (event `payload["latency_ms"]`, else created→captured gap) | 5 min | **up** |
| `checkout_abandonment_rate` | abandoned / decidable checkout attempts created in the bucket | 30 min | **up** |
| `insufficient_fund_share` | insufficient-funds failures / all failures | 60 min | **up** |

Buckets with fewer than `min_bucket_count` (default 5) events carry no
statistical signal and are skipped — with 2 payments in a bucket, one failure
is a 50pp swing, not an incident. The two share metrics carry their own
count floors (`insufficient_fund_share`: 2 failures; `checkout_abandonment_rate`:
10 decidable attempts) applied unless the request sets `min_bucket_count`
explicitly — that is what lets them work in the small-volume night regime the
global floor was designed to suppress.

**`checkout_abandonment_rate` is attempt-based.** Abandoned checkouts stay
`created` and never produce terminal events, so outcome-based series are
blind to them by construction. The engine resolves every payment *created*
in the window against a 30-minute inactivity threshold (no terminal outcome
within `created + 30m` = abandoned). Right-censoring is handled honestly:
the pass's knowledge edge is the window end, so attempts whose threshold
horizon falls beyond it are excluded from numerator AND denominator (the
last 30 minutes of a window simply carry less signal), and no event after
the window end is ever consulted.

**`insufficient_fund_share`** is the failure *mix*, not the failure count:
the insufficient-funds share of failed terminal outcomes per bucket
(defensive substring match on `error_reason` — Razorpay telemetry has no
closed enum). It exists for the sparse regime where the success rate itself
cannot be scored: at night a bucket may hold 1–5 outcomes total, but when 3
of them fail and all three are insufficient funds, the mix is the signal.

### Segments

Every payment carries segment dimensions used to *localize* a degradation:

- `method` — `Payment.method` (`upi`, `card`, `netbanking`, ...)
- `bank` — `Payment.meta["bank"]`
- `gateway` — `Payment.meta["gateway"]` (default `"razorpay"`)
- `route` — `Payment.meta["route"]` (e.g. `pg_primary`; Razorpay
  Optimizer-style gateway route)

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
  "metrics": ["payment_success_rate"],   // default: all four known metrics
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
| `min_absolute_deviation` | per metric: **5pp** success rate, **75 ms** latency, **20pp** abandonment share, **25pp** error share (`null` in the request selects the default; `0` disables) | `|observed − baseline|` in metric units. Guards quiet-merchant hair-triggers: with the baseline std floored at 1% of mean, a 2–3pp wobble can exceed 3σ on a near-perfect baseline. |
| `min_flagged_volume` | **15** events; **3** failures for `insufficient_fund_share` (metric default) | total events across the flagged buckets. A "50% drop" built on 6 payments is not an incident. |
| `min_flagged_run` | **2** consecutive buckets; **1** for `insufficient_fund_share` (metric default) | persistence: the degradation must hold for at least 2 adjacent flagged buckets (≥ 10 min at the default grid). One bad bucket is a blip. Note: `isolation_forest` flags boundary buckets by design — pair it with `min_flagged_run: 1`. |
| `min_observed` | **0.35** abandonment share, **0.90** error share (engine constants, up-direction share metrics only) | the anomaly's worst bucket must reach the level itself: a *wave* means the mix is dominated by the signal class, not merely elevated. Measured: organic insufficient-fund clusters peak at 0.71 share (z up to 7); natural abandonment clumps at ≤ 0.2. |

Per-metric floor defaults apply only when the request does not set the floor
explicitly (`model_fields_set`); an explicit request field always wins.

**Opt-in night-regime floors (`insufficient_fund_share` only).** The request
knob `night_regime_floors: true` (default **false** — the published operating
point) swaps the share/absolute bars for an anomaly whose flagged buckets
**all** sit inside the night band (engine constants: UTC hour ≥ 18 or < 1,
i.e. 18:00–01:00 UTC ≈ 00:00–06:30 IST, the measured night trough): the
`min_observed` bar drops 0.90 → **0.60** and the absolute-deviation floor
25pp → **15pp**. Rationale, from the published measurements: the 0.90 bar
exists only because organic *daytime* clusters reach 0.71 share — at night
the mix has no such competitor, while the injected wave's failures spread
across trough buckets and dilute under 0.90 (docs/evaluation.md §3b note 2).
One daytime bucket in the episode disqualifies the night set, so organic
daytime clusters are judged by the global floors in both modes; the
volume/persistence floors and every detector are untouched. Incidents
admitted under the night set are stamped `meta.night_regime_floors: true`
(and the run detail carries `night_regime_floors=on`) so downstream readers
can tell them apart from the published operating point. The values are
chosen from the published measurements and validated on synthetic fixtures
(`backend/tests/detection/test_night_regime_floors.py`) — disclosed, not
re-tuned on the anchor suite.

Fires that fail a floor are counted in the run response as
`anomalies_filtered` (and logged with the failed floor), not persisted.
Every floor is request-configurable, so threshold tuning via `dry_run` can
see exactly what would be admitted.

Measured on the standard-scenario harness (scheduled 12h/6h passes,
production defaults, seed 42 — see "Measured effect" below): the binding
floors on that dataset are **persistence** and **flagged volume**; the
absolute-deviation floor does not bind there (organic noise deviates far
more than 5pp) and exists for the small-sample regime.

## Per-route latency scan (blind-spot cover)

The `route_latency` blind spot is not that a route barely moves the aggregate
— measured on the standard dataset, merchant-wide mean capture latency jumps
~6–10x during the injected incident. The z-score stays silent because the
incident's early-morning buckets are sparse (1–5 captures) and the pass whose
window starts inside them builds its leading baseline from incident buckets
(baseline poisoning). The aggregate therefore never fires, and localization
(which runs *after* a fire) never gets the chance to say which route it was.

When a pass admits **no** merchant-wide `capture_latency_ms` incident for a
detector, the engine re-runs that detector on per-`route` slice series
(15-min buckets, count floor 3 — a route carries a fraction of traffic) with
the standard floors. Scanning only when the aggregate is silent means
fleet-wide latency incidents (gateway_degradation) never produce duplicate
slice incidents.

Slice fires face one extra admission gate, **within-method corroboration**:
a real route degradation slows *every* method flowing over the route, while
a method-mix shift only moves the aggregate mean. The rise must hold within
methods — at least two methods with ≥ 3 samples in both the pre-anomaly and
anomaly regions must each rise ≥ 2x (a lone well-sampled method must rise
≥ 3x). Measured: the injected incident rises ≥ 7x in every method; organic
slice fires (4 in an unguarded run) rose in at most one method and are all
rejected by this gate. Admitted slice anomalies persist with
`segment={"route": ...}` and `meta.segment_scan=true`.

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
numbers shift with the calendar day of the run: the pre-fix published
reading (precision 0.185 / recall 0.833, since superseded) was measured on a
dataset anchored 2026-08-26, this before/after pair on 2026-08-27, and the
current published runs (docs/evaluation.md §3/§3b) span the 2026-08-27 and
2026-08-28 anchors. The before/after *delta* below is what is reproducible —
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

(Downstream-row provenance: this pair predates the randomized-holdout arm.
With the holdout withholding ~9–10% of customers, the same post-floors
detection state reads 655 / 60 / 5 — the number of record, in
docs/evaluation.md §3b (`run_caa1f1a9…`, "detection v1 + first holdout
arm"). Detection precision/recall/F1/MTTD are fleet-wide and unaffected by
the holdout, so those rows agree across both readings.)

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
floor casualties — `route_latency` (sparse early-morning buckets + leading-
baseline poisoning keep the merchant-wide z-score silent even though the
aggregate latency jumps ~6–10x — measured, see the scan section),
`checkout_abandonment_spike` (abandoned checkouts never become terminal
outcomes, so the success-rate series is blind by construction), and
`customer_insufficient_funds_wave` (runs 00:00–20:00 IST; its night buckets
carry 1–5 events and fall under `min_bucket_count`, so the signal never
enters the scored series — verified on the persisted series snapshot).
All three are closed by the recall attack measured below.

## Measured effect of the recall attack (new signals)

Before/after on the **same dataset and seed**: scenario `standard` (30 days,
~69k payment_events, 6 injected incidents), seed 42, scheduled 12h/6h passes
at production defaults, reproduced by
`scripts/run_evaluation.py --scenario standard --seed 42`
(before: run `run_4f3b346e88e74d3d91c4fba2c2caa94a`; after: run
`run_0022000d8df942e6ac4b7299986f994a`; both anchored 2026-08-27 — absolute
numbers shift with the calendar day of the run, the same-day delta is what is
reproducible). Three additions, each validated on synthetic fixtures (quiet
control + injected spike), then measured on the harness:

1. **Per-route latency scan** with within-method corroboration (above).
2. **`checkout_abandonment_rate`** — attempt-based, right-censoring-aware.
3. **`insufficient_fund_share`** — the failure-mix signal for the sparse
   night regime, with near-single-class admission (see exp003).

| metric | before | after |
|---|---:|---:|
| incident rows persisted | 6 | **9** |
| matched rows / ground truth | 4 / 3 of 6 | 7 / **6 of 6** |
| **precision** | 0.667 | **0.778** |
| recall | 0.500 | **1.000** |
| F1 | 0.571 | 0.875 |
| MTTD (min, sim time) | 895 | **585** |
| per-kind recall | 3/6 kinds | **6/6 kinds** |
| downstream: opportunities / interventions / false interventions | 655 / 60 / 5 | 903 / 90 / 7 |
| recovered revenue (harness) | ₹13,807 | **₹24,529** (+77.7%) |
| unsafe actions | 0 | 0 |

The two pre-existing organic success-rate FPs (08-15, 08-22) remain — no new
signal touched that path, and the new signals added **zero** false positives
on this dataset (validated on the quiet scenario too: the before/after quiet
replays produce the identical 6 pre-existing organic incidents). Newly
surfaced ground-truth revenue at risk: route_latency ₹51,727 (not
recoverable), checkout_abandonment ₹69,201 (recoverable),
insufficient-funds wave ₹104,458 (recoverable).

Evidence: `ml/experiments/detection/exp001..exp003` (config, metrics,
failure analysis per signal, including the rejected tuning iterations and
the payday-scale limitation of the error-share signal).

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
  nothing (accepted trade-off of baselines-first). The error-share metric
  sidesteps this for the insufficient-funds wave only in the ONE pass that
  straddles the wave start. The structural fix now exists as an OPT-IN:
  `baseline_mode: "same_time_yesterday"` on the run request builds the
  baseline from the same clock window shifted back 24h (implemented in
  `app/services/detection/engine.py`; tested in
  `backend/tests/detection/test_same_time_yesterday.py`). It defaults OFF —
  every number above was measured with the leading window, so the published
  operating point stays the default.
- Sparse traffic: buckets under `min_bucket_count` are skipped; very quiet
  merchants get no detection rather than noisy detection. The two share
  metrics carry their own lower count floors (measured above), which moves
  the boundary but does not remove it: abandonment spikes under ~10
  decidable creations per 30-min bucket are not scored, and the
  `insufficient_fund_share` signal misses the wave at smaller scale —
  measured on payday_wave_demo (25k events/14 days): the wave's only
  near-pure hour carries 2 failures (under the 3-failure volume floor) and
  its richer hours top out at 0.78 share, under the 0.90 admission bar that
  organic clusters (0.71) force. Recall 0/1 there, zero false positives.
- The `insufficient_fund_share` 0.90 bar is deliberately narrow: organic
  daytime insufficient-fund clusters reach 0.71 share on 7 failures (z up to
  7) — statistically *stronger* than the wave's night bucket (3 failures,
  share 1.0, z 3.2). Only near-single-class hours are admitted; a wave whose
  night band never produces one is missed (see
  `ml/experiments/detection/exp003/failure_analysis.md`). The structural
  follow-up now exists as an OPT-IN: `night_regime_floors: true` on the run
  request judges an all-night anomaly by a lower share/absolute floor set
  (0.60 / 15pp; implemented in `app/services/detection/engine.py`, tested in
  `backend/tests/detection/test_night_regime_floors.py`). It defaults OFF —
  every number above was measured with the global floor set, so the
  published operating point stays the default.
- The route scan can admit a strong organic latency episode when it rises
  across methods (measured once on standard/seed7: dev +907%, 1h span) —
  corroboration kills mix shifts, not genuine organic multi-method latency
  events.
- Abandoned-checkout incidents now feed recovery: the OpportunityBuilder
  sources payments stuck in `created` as `stuck_checkout_payment`
  opportunities (payment-link-first strategy; see docs/recovery.md §2).
- Seasonality: the leading-window baseline cannot tell a daily-cycle trough
  from a degradation. The noise floors + episode dedup suppress the
  resulting incidents (measured above), but multi-hour organic swings still
  occasionally admit (2 residual FPs in the measured run). The real fix is
  implemented and opt-in: `baseline_mode: "same_time_yesterday"` compares
  each bucket against yesterday's same hours (a daily dip compares against
  yesterday's dip and stays silent; a genuinely new degradation against a
  healthy yesterday still fires). It needs >= 4 decidable baseline buckets
  yesterday or the metric stays silent for the pass, and it defaults OFF to
  preserve the measured operating point above.
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

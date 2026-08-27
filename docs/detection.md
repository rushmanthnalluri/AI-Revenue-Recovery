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
  "dry_run": false
}
```

The window is anchored at the **latest terminal event** (or `as_of`), not
wall-clock now — identical data yields an identical window, which is what
makes re-runs deterministic and idempotent.

For every (metric × detector) anomaly the engine persists:

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

**Idempotent re-runs:** the dedup key is `(metric, detection_method,
window_start, window_end, segment_fingerprint)`. Re-running the same
(window, segment, detector) **updates** the existing incident — latest
observed/deviation/severity/impact, fresh evidence (replaced, not stacked) —
and deliberately preserves the original `detected_at` and leaves `status`
untouched (a human's triage is never clobbered by a re-run).

**Detection latency** is computable from the persisted record:
`detected_at` (when the engine saw it) vs the simulator ground-truth start
(evaluation agent), and `meta.anomaly_start` (estimated start) for the
engine's own estimate. `meta.bucket_minutes` converts either to buckets.

`dry_run: true` computes everything and persists nothing
(`action: "would_create"`), for threshold tuning against live data.

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
  nothing (accepted trade-off of baselines-first).
- Sparse traffic: buckets under `min_bucket_count` are skipped; very quiet
  merchants get no detection rather than noisy detection.
- IsolationForest needs ≥ 8 baseline buckets and adds no value below ~20
  total buckets.
- Severity is a function of deviation magnitude only; business weighting
  (revenue mix per segment) is the downstream risk agent's job.
- `incident_evidence.evidence_type` adds `segment_breakdown` alongside the
  model comment's example list (`metric_series` was already listed) — the
  column is a free varchar, no migration needed.

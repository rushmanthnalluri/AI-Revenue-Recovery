# Flows D/E/F — Detection, Diagnosis, Revenue-at-Risk

Audit Phase 4 evidence file. Captured 2026-09-02 by audit agent (flows D/E/F).
Status vocabulary: WORKING / PARTIALLY_WORKING / BROKEN / MOCKED / SIMULATED / UNIMPLEMENTED / UNCERTAIN.
Every claim carries file:line or command evidence. Deltas/uncertainties flagged inline.

## Section map

1. Flow D — Detection pipeline (events → series → baseline → detectors → floors/dedup/cooldown → incident → UI)
2. Flow E — Diagnosis pipeline (incident → features → served artifact → confidence → heuristic fallback → LLM seam → report)
3. Flow F — Revenue-at-risk (estimated/observed/recoverable/verified/incremental; priors; stuck-checkout fallback; env scoping)
4. Misleading terminology register
5. Findings summary (severity-tagged)

---

## 1. Flow D — Detection pipeline

**Overall status: WORKING (as engineered, on-demand only) — but effectively DORMANT on the live real_test deployment** (no scheduler, webhook-only event stream with almost no real_test events; see 1.1, 1.8).

### 1.1 Payment event stream ingestion — the critical dependency

- Detection consumes the `payment_events` table, NOT the `payments` table: module docstring `backend/app/services/detection/series.py:1` ("Time-bucketed metric series built from the ``payment_events`` stream") and `engine.py:1`.
- `PaymentEvent` rows have exactly TWO writers in the codebase:
  1. Webhook intake: `backend/app/services/recovery/webhook_handlers.py:298-309` (`_transition_payment`, `source="webhook"`, provenance `razorpay_test` only when the gateway is real, lines 269-286).
  2. The simulator: `backend/app/simulator/engine.py:523,711,964` (source_type `simulator`).
  - Grep evidence: `PaymentEvent(` appears only at `webhook_handlers.py:299` and simulator engine sites; the REST **sync path creates NO `payment_events` rows**. Verified via `grep -n "PaymentEvent(" backend/app` (only those two writer families plus model/detection reads).
- Consequence for real_test: payments synced from Razorpay Test Mode via REST have **no terminal events**, so detection over `real_test` finds nothing unless webhooks actually delivered `payment.captured/authorized/failed` for those payments. Engine behavior with an empty stream: `run_detection` returns early with detail `"no terminal payment events in scope; nothing to detect"` (`engine.py:280-284`) unless an explicit `as_of` anchor is passed. Even the attempt-based checkout-abandonment metric (built from `Payment` rows, not events — `series.py:221-295`) is gated behind the event anchor when `as_of` is omitted, because the window is anchored on the latest terminal event.
- Environment boundary: `real_test` = source_types (`razorpay_test`, `razorpay_live`); `research` = (`simulator`,) — `backend/app/models/base.py:52-70` (`source_types_for_environment`). The detection pass scores only the request environment's rows and stamps `environment` on incidents/evidence (`engine.py:274-276,1031`). Request field default is `real_test` (`backend/app/schemas/detection.py:45`).

### 1.2 Series construction

- Window: anchored at the latest terminal event (data-anchored, deterministic) or explicit `as_of`; `[window_start, window_end)` aligned to the bucket grid — `engine.py:280-292`. Defaults: `window_minutes=60` (max 24h), `bucket_minutes=5` — `schemas/detection.py:12,18`.
- Four metrics (`series.py:41-50`):
  - `payment_success_rate` — share of terminal outcomes captured/authorized vs failed; degrades DOWN (`series.py:5-6,53-58`).
  - `capture_latency_ms` — mean capture latency per bucket; payload `latency_ms` else captured-event time minus payment creation (`series.py:298-309`); degrades UP.
  - `checkout_abandonment_rate` — share of checkout *attempts* (payments created) with no terminal outcome within `ABANDONMENT_INACTIVITY_MINUTES=30` (`engine.py:176`); attempt-based; right-censoring handled tri-state (undecidable attempts excluded from numerator AND denominator — `series.py:392-431`); degrades UP.
  - `insufficient_fund_share` — share of *failed* outcomes whose error reason substring-matches `insufficient_fund|insufficient_balance` after normalization (`series.py:74-81`); degrades UP.
- Outcome resolution: latest terminal event per payment wins (failed→captured is legitimate per Razorpay semantics) — `series.py:196-211`.
- Sparse-metric bucket multipliers: abandonment 6× (30-min buckets), insufficient-fund share 12× (60-min buckets) — `engine.py:127-130`.

### 1.3 Baseline computation

- Default mode `leading_window`: the first `baseline_buckets` (default 8, schema min 4 — `schemas/detection.py:19`) *valid* buckets of the analysis window are assumed healthy and define mean/std (`detectors.py:87-98`); std gets a relative floor `max(sigma, |mu|*0.01, 1e-9)` so a pinned 1.0 success rate is not hair-trigger.
- Opt-in `same_time_yesterday`: baseline records are the same clock window shifted back 24h; series = yesterday + today, only today's buckets scored; requires ≥4 (`STY_MIN_BASELINE_BUCKETS`) decidable yesterday buckets or the metric stays silent for the pass — `engine.py:193-201,300-376`.

### 1.4 Detectors (registry `detectors.py:375-397`; default request detector = `zscore`, `"all"` runs all four)

| Detector | Algorithm | Default threshold | Notes |
|---|---|---|---|
| `zscore` | rolling-baseline z-score | 3.0σ (`detectors.py:177-178`) | fastest on steps; weak on drift (docstring) |
| `ewma` | EWMA control chart, λ=0.3 | L=3.0 (`detectors.py:211-213`) | time-varying limit; catches smaller sustained shifts |
| `cusum` | one-sided CUSUM, k=0.5σ | h=4.0σ (`detectors.py:252-254`) | statistic capped at h+k so alarm clears after recovery |
| `isolation_forest` | sklearn IsolationForest on [value, Δ, dev from trailing-3 mean] | contamination 0.25, gate 2.5σ (`detectors.py:299-302`) | fitted over whole window; 3 gates: post-baseline only, direction vs baseline median, magnitude ≥ 2.5σ |

- Common contract: only buckets with `count >= min_bucket_count` scored (default 5; per-metric overrides: insufficient-fund share 2, abandonment 10 — `engine.py:142-148`); direction-aware; `sensitivity` (default 1.0) divides the threshold (`detectors.py:110-112`); each detector returns at most ONE anomaly (first flagged bucket = `start_ts`, worst bucket = observed/deviation — `detectors.py:125-147`).
- Severity mapping: |deviation_pct| ≥50 CRITICAL, ≥25 HIGH, ≥10 MEDIUM, else LOW — `engine.py:249-258`.
- Segmentation/localization: per-dimension slices (`method`/`bank`/`gateway`/`route` — `series.py:70`) re-scored inside the anomalous region; top 3 per dimension ranked by |deviation|; `flagged` = deviation in degradation direction ≥ half the global deviation — `engine.py:827-880`. Segment values come from the payment row (`method` column; `bank`/`gateway`/`route` from `Payment.meta`, gateway defaulting to `"razorpay"`) — `series.py:138-145`.

### 1.5 Noise handling: floors / dedup / cooldown

Incident-level floors are engine-side (detectors stay pure statistics) — `engine.py:520-561`:
- `min_absolute_deviation` per metric: success rate 0.05 (5pp), latency 75ms, abandonment 0.20, IF share 0.25 (`engine.py:102-107`).
- `METRIC_MIN_OBSERVED` admission bar on the observed level (up-share metrics only): abandonment 0.35, IF share 0.90 — "a wave must dominate the mix" (`engine.py:116-119`). Comments cite measurements on the standard/seed42 harness (organic daytime IF clusters peak 0.71 share; injected wave night bucket 1.0).
- `min_flagged_volume` (default 15 events; IF share 3 — `engine.py:150`) and `min_flagged_run` (default 2 consecutive buckets; IF share 1 — `engine.py:149`).
- Fires failing a floor are counted in `anomalies_filtered` and dropped with a logged reason (`engine.py:588-600`).
- Route-latency blind-spot scan: when the merchant-wide latency pass admits nothing, per-route slices are re-scored (enabled `engine.py:185`; 15-min slice buckets, min bucket count 3, slice must be <95% of outcomes `engine.py:187-191`) plus a **mix-shift corroboration guard**: the latency rise must hold within ≥2 methods at ≥2× (or one method ≥3×) — `engine.py:647-688`.
- Dedup/cooldown/suppression (`engine.py:930-996`, `_persist` 999-1206):
  - Same-window re-run of same (metric, detector, environment, segment_fingerprint) → UPDATE (original `detected_at` preserved, evidence refreshed).
  - Cross-window episode merge: OPEN incident with same signature whose anomaly span overlaps or lies within `dedup_cooldown_minutes` (default 360) is merged; earliest `detected_at` kept; `merge_count` incremented; span widened, never narrowed (`engine.py:1121-1131`).
  - Post-resolution suppression: re-detection of a signature resolved (RESOLVED/CLOSED/FALSE_POSITIVE) within `suppress_after_resolve_minutes` (default 720) → action `suppressed`, nothing persisted (`engine.py:972-996`).

### 1.6 Opt-in modes (both default OFF — verified `schemas/detection.py:51,60`)

- `baseline_mode="same_time_yesterday"` — default `leading_window` (`schemas/detection.py:51`). Schema comment: leading_window is "the published operating point".
- `night_regime_floors=False` default (`schemas/detection.py:60`). When ON, an all-night `insufficient_fund_share` anomaly (every flagged bucket in UTC hour ≥18 or <1 — the engine-fixed approximation of 00:00–06:30 IST, `engine.py:168-169,479-499`) is judged by lower bars: `NIGHT_MIN_OBSERVED` 0.60 (vs 0.90) and `NIGHT_MIN_ABSOLUTE_DEVIATION` 0.15 (vs 0.25) (`engine.py:170-171`). Any daytime bucket in the episode disqualifies it (`engine.py:479-488`). Admitted incidents carry an honesty marker `meta.night_regime_floors: true` (`engine.py:610-613`). Comments state the published evaluation anchors (docs/evaluation.md §3b) were all measured with this mode OFF.

### 1.7 Incident creation

- One `incidents` row per (metric, detector, window, segment) with status OPEN, severity from deviation magnitude, `environment` stamped, `revenue_at_risk_paise` = **preliminary** impact = sum of amounts of affected payments (failed / slow-above-baseline / abandoned / insufficient-fund) from anomaly start to window end — `engine.py:883-902,1141-1167`. This is a raw sum of payment amounts, later overwritten by the revenue service's counterfactual estimate on incident detail view (see Flow F; `backend/app/api/v1/incidents.py:6-9`).
- Evidence attached per incident: `metric_series` snapshot (all buckets) + `segment_breakdown` ranking, collector `agent:detection` — `engine.py:1209-1246`. On re-run, old detection evidence rows are deleted and re-added (`engine.py:1180-1185`).
- Every non-dry-run pass writes an audit row `detection.run` — `backend/app/api/v1/detection.py:43-66`.
- `dry_run` computes everything, persists nothing (`engine.py:435-436`).

### 1.8 UI surfacing + triggering

- Trigger surface: **on-demand only**. `POST /api/v1/detection/run` (`backend/app/api/v1/detection.py:23-42`). Exempt from X-API-Key only when `APP_ENV != prod` (`backend/app/main.py:43-44,135`; live deployment is `APP_ENV=prod` per baseline.md → the live endpoint requires the API key).
- **No scheduled detection exists**: the in-process worker runs only delayed retries, notification outbox, and reconciliation — `backend/app/services/worker/worker.py:1-27` (docstring enumerates exactly three units; grep for `detection` in `backend/app/services/worker` returns nothing). Detection never runs unless someone calls the API.
- Demo path: `POST /api/v1/demo/scenario/{name}` seeds a simulator scenario and runs ONE anchored detection pass pinned to `environment="research"` (`backend/app/api/v1/demo.py:207-228`). The frontend demo control states "Research data only; the real merchant environment is never touched" (`frontend/src/components/demo-control.tsx:85`).
- UI consumption: incident register `/incidents` → `GET /api/v1/incidents` (`frontend/src/lib/api.ts:265`; list view `frontend/src/components/incident/incident-list-view.tsx:121`), detail `/incidents/[id]`. **No frontend control triggers a real_test detection run**: the API client has a `detection.run` method (`frontend/src/lib/api.ts:371-374`) but no component calls it (grep `api.detection` across `frontend/src` → only the definition and an unrelated evaluation parser). It is a dead client seam.

---

## 2. Flow E — Diagnosis pipeline

**Overall status: WORKING (ML artifact served on the live deployment) — with the same event-stream dependency as Flow D, a heuristic-fallback claim (91.5%) that does NOT generalize beyond a toy generator, and one cross-environment feature-leak seam.**

### 2.1 Trigger — lazy, not automatic

- Diagnosis is produced on FIRST VIEW of an incident detail: `GET /api/v1/incidents/{id}` → `_ensure_diagnosis` runs `DiagnosisService.classify` when no diagnosis exists (`backend/app/api/v1/incidents.py:4-6,165-182`). Failure rolls back and the detail still renders (no diagnosis).
- Second trigger: `POST /api/v1/incidents/{id}/investigate` (the agent report) ensures a diagnosis first (`backend/app/services/agent/service.py:3-9`).
- Nothing runs diagnosis at incident-creation time; the evaluation harness also calls it (`backend/app/services/evaluation/runner.py:12`).

### 2.2 Features (58-float vector)

- `FEATURE_NAMES` (58 names) at `backend/app/services/diagnosis/features.py:73-119`: volume/headline rates, per-method and per-bank failure-rate deltas, error source/step/reason share-of-failure deltas, latency p50/p90 deltas, abandonment proxy, subscription features.
- Frame: incident window `[window_start, window_end)` vs an equal-duration baseline immediately preceding it (`features.py:187-227`). Records come from `payment_events` joined to `payments`, latest event in window decides outcome (`features.py:137-184`) — **the same webhook-driven event stream as Flow D; an incident whose window has no events yields an all-zero-ish feature vector, not an error**.
- **FINDING (cross-environment leak): `load_window_records` (`features.py:144-149`) has NO `source_type`/environment filter** — unlike the detection engine (`engine.py:274-276`). A diagnosis window overlapping the OTHER environment's events (e.g. simulator rows timestamped inside a real_test incident window) would silently mix both environments into the feature vector. In practice real_test has almost no events (§1.1), so contamination is latent, not observed. The window re-scoping triage is environment-scoped (ml.md:260-268) but that only tightens the frame; feature records are still loaded unfiltered.
- Window re-scoping triage (`rescope.py`): tightens the detection frame to the floor-breaching span before feature computation. **Default OFF** (`service.py:18-23,73-76`; env `DIAGNOSIS_WINDOW_RESCOPE`), and ml.md:275-278 states it is unit-tested but NOT re-anchored — readings with it on are not canonical.

### 2.3 Served artifact — verified

- Pointer `backend/artifacts/diagnosis_active.json` (read at `service.py:92` via `training.py:592-602`): artifact `diagnosis_random_forest_v20260828T013109Z-77a4ef3b.joblib`, algo `random_forest`, calibration `none`, model_version `v20260828T013109Z-77a4ef3b`, trained_at 2026-08-28. Pointer-recorded metrics: val macro-F1 0.7401, **test macro-F1 0.6293, test top-1 0.7154, test top-3 0.9308**, test ECE 0.1291, Brier 0.459, safe-auto-lane coverage 0.2708.
- The joblib file exists in `backend/artifacts/` (directory listing). Both pointer and the RF joblib (plus the rollback LR `v20260826T234303Z-c5434878`) are committed to git — `.gitignore:29,34-36` ignores `**/artifacts/*` then un-ignores exactly these three files; `git ls-files backend/artifacts` confirms. `deploy/Dockerfile.backend` (`COPY backend/ ./`) bakes them into the image → **the live Render deployment serves the RF artifact, not the heuristic** (status: WORKING, assuming joblib loads under the image's python 3.12 / sklearn 1.9.0 — pinned in requirements per baseline.md:18; not independently re-verified live).
- Inference: `predict_proba` aligned to canonical label order; confidence = top-1 probability; top-3 persisted (`service.py:175-196`). Model name `diagnosis-random_forest`.
- Training governance (docs/ml.md §8–§10, experiment records `ml/experiments/diagnosis/`): temporal split, val-rule selection, and a pre-registered ship gate. The shipped RF REPLACED the §8 LR after exp06 NO-SHIP/rollback and exp07's pass (ml.md:394-439). Disclosed weaknesses on record: prod-frame auto_coverage dropped to 0.364 (more hedging into the approval lane, ml.md:460-461); exact-span macro-F1 0.7664 vs incumbent 0.8231 with bank_downtime F1 0.000 at support 15 (ml.md:450-456); a stricter pre-registered continuity clause FAILS (0.7664 < 0.7931) and the ship call followed the exp06 top-1 clause instead (ml.md:454-458).
- **Doc-number caution:** docs/product-strategy.md:65 quotes "0.910/0.995" for the active model — those are the *exact-span* frame readings (ml.md:447 top-1 0.9098); the pointer's own production-frame test numbers are 0.7154/0.9308. Different frames, both on record — but the higher pair is the one that made the strategy doc.

### 2.4 Heuristic fallback — and the 91.5% claim

- Used ONLY when the pointer/artifact is missing or unloadable (`training.py:592-602`, `service.py:92-97`). Rules: `backend/app/services/diagnosis/heuristic.py:27-97` — ordered threshold rules (subscription → abandonment → method → bank → insufficient-funds → latency → gateway), confidence capped at 0.70/0.60/0.45 (`heuristic.py:24`), remaining mass spread uniformly (explicitly NOT calibrated, `heuristic.py:91-95`), flagged `heuristic=true`, model `diagnosis-heuristic@heuristic-1`, explanation prefixed `[heuristic]`. Cap 0.70 < 0.85 auto floor → heuristic can never enter the auto-execute lane (ml.md:320-322: "perfectly safe, covers nothing").
- **The 91.5% claim — VERIFIED AS STATED, MISLEADING IN CONTEXT.** docs/ml.md:132: "Heuristic fallback (cold start, no artifact): 91.5% top-1 on the same generator (439/480 windows, 60/class)". "The same generator" = the PRELIMINARY `--synthetic` toy generator whose own §5 reading admits the signatures are linearly separable and near-perfect for every ML algo (ml.md:126-130). On realistic simulator production frames the same `heuristic-1` rules score **top-1 0.3922 / macro-F1 0.1830** (ml.md:320-321, v2 test n=102), 0.4402/0.2232 (`ml/experiments/diagnosis/exp02_baselines_prod_frames/metrics.json` full block), 0.4625/0.2424 (`ml/experiments/diagnosis/exp05_final_selection_v2/baselines/metrics.json` full block). docs/claim-matrix.md:143 marks 91.5% "Attested" (record exists), not reproduced. So: the number is honestly recorded WITH its dataset, but any reading of "91.5% fallback accuracy" as production-representative overstates the fallback by ~2×.

### 2.5 Confidence → action gating

- `AUTO_EXECUTE_CONFIDENCE_FLOOR = 0.85`, `ESCALATION_CONFIDENCE_THRESHOLD = 0.5`, `NON_AUTO_CONFIDENCE_CAP = 0.84`, `THIN_EVIDENCE_WINDOW_FLOOR = 10`, `RANKED_CANDIDATE_LIMIT = 3` — `backend/app/services/agent/report.py:23-45`.
- Report confidence is a deterministic evidence-calibrated formula (diagnosis conf ± evidence sufficiency adjustments, clamped [0.05, 0.95]) — `reasoners.py:123-144`. For diagnoses outside `AUTO_RECOVERABLE_CAUSES` (`taxonomy.py:53-59`: only gateway_degradation/method_outage/bank_downtime are auto-eligible), the gate input is capped at 0.84 (`reasoners.py:147-156`) — motivation on record: the old artifact crossed 0.85 on 52.8% of non-auto-recoverable prod frames (ml.md:324-327).
- Escalation triggers: no diagnosis / empty window / no candidates / confidence < 0.5 (`reasoners.py:606-625`).

### 2.6 LLM seam — UNTESTED in this deployment

- `choose_reasoner` (`reasoners.py:1117-1133`): `LlmReasoner` only when `LLM_PROVIDER=openai` AND `OPENAI_API_KEY` set; otherwise `HeuristicReasoner`. Config default `LLM_PROVIDER="none"` (`backend/app/config.py:38-42`); baseline.md:42 confirms none configured live → **all live reports are heuristic-reasoner, deterministic** (`reasoners.py:189-592`).
- The LLM path (bounded 6-iteration tool loop, temperature 0, 2 attempts, whitelist + JSON-schema + hallucination guards, numbers only from tools, fallback-to-heuristic marked `degraded` — `reasoners.py:632-790,794-1000`) is unit-testable via `chat_fn` injection but has never run against a real provider in this deployment. Status: UNIMPLEMENTED operationally (code present, seam dark).

### 2.7 Report + persistence

- Every classify persists a `diagnoses` row (label, confidence, features JSON, explanation, auto-incremented version) + a `model_predictions` row (full proba, top-3, `heuristic` flag, rules fired, both window frames) (`service.py:99-135`), and fills `incident.root_cause` (`service.py:113-116`) — status transitions deliberately untouched.
- Investigation reports persist to `agent_reports` + audit row, one commit (`service.py:10-11`). Advisory only — no financial action executes on this path (`service.py:13-14`); every recommended action is dry-run through the live policy gate and BLOCKED proposals are dropped (`reasoners.py:485-499,938-967`).
- UI: incident detail view renders diagnosis + investigation (`frontend/src/components/incident/incident-detail-view.tsx`, `incident-audit-timeline.tsx`); API `frontend/src/lib/api.ts:267-274`.
- Doc claim check: docs/evaluation.md:235 "diagnosis top-1 1.000, top-3 1.000 (4 scored)" — claim-matrix.md:112 re-ran it and confirms, with the small-n caveat in place. n=4; treat accordingly.

---

## 3. Flow F — Revenue-at-risk

**Overall status: WORKING (methodology honestly implemented, environment-scoped, read-only) — every "recoverable"/"expected" number is a documented PRIOR, not a measurement; only `actual_recovered` is measured.**

### 3.1 The five distinct numbers (they are NOT interchangeable)

| Name | What it is | Evidence |
|---|---|---|
| detection `revenue_at_risk_paise` | GROSS sum of affected payment amounts (failed/slow/abandoned/IF) from anomaly start to window end — no counterfactual | `backend/app/services/detection/engine.py:883-902`, persisted at 1141-1167 |
| `observed_loss` | COUNTERFACTUAL estimate: per-segment (method × amount-band × new/returning) baseline success rate over the 7-day pre-incident window; `loss = max(0, attempted × baseline_rate × baseline_AOV − captured)` | `backend/app/services/revenue/engine.py:429-471`, segmentation 405-412, baseline window `config.py:34` |
| `recoverable` | observed_loss allocated to failure classes by share of failed amount, × recoverability PRIOR per class | `engine.py:473-491`, priors `config.py:68-89` |
| `expected_recovery_by_strategy` | recoverable × strategy-effectiveness PRIOR (retry 0.50, link 0.30, resume_sub 0.25, notify 0.15, grace 0.10, escalate 0.05; pause/refund/no_action exactly 0) | `engine.py:200-207`, `config.py:94-113` |
| `actual_recovered_paise` ("verified") | MEASURED: sum of `recovery_actions` in status RECOVERED (webhook-verified) only; UNKNOWN counted separately, never included | `engine.py:512-521`, dashboard variant 319-365, `types.py:123,156-168` |

- `incremental` lift exists ONLY in the evaluation harness (randomized customer-level holdout; lift = treatment recovery rate − holdout rate): `backend/app/services/evaluation/holdout.py:1-15`, `runner.py:33,494-599`, surfaced at `backend/app/schemas/evaluation.py:51-53`. It is NOT part of the production incident revenue API — no production number is incremental-lift-adjusted.

### 3.2 Uncertainty handling

- Only probabilistic quantity: the per-segment baseline success rate → Wilson score interval (z=1.96) (`backend/app/services/revenue/statistics.py:20-35`); bands propagate by evaluating the formula at the rate bounds (`engine.py:460-461`). Cross-segment aggregation sums band endpoints (deliberately worst-case-correlated, wide not falsely tight — `engine.py:80-115`).
- Confidence = linear ramp `min(1, n/200)` (`statistics.py:38-47`, `config.py:52`); `low_confidence` when baseline n<30 or confidence<0.5 (`engine.py:462`, `config.py:48`).
- Zero-baseline segment: NO point estimate (point=None), band spans full attempted volume, AOV falls back to the window's own mix, confidence 0.0 (`engine.py:450-456`). Aggregate basis discloses "N component(s) had zero baseline signal" (`engine.py:103-107`).
- Single-opportunity estimates: band is the full [0, amount], confidence 0.3, `low_confidence` always True — "these numbers rank strategies, they do not promise revenue" (`engine.py:237-248,275-285`, `config.py:60`).
- Pending (non-terminal) payments are excluded from rates AND from loss volume (`engine.py:51-64,152-155`).

### 3.3 Recoverability PRIOR table — not measured

`config.py:68-89`: timeout 0.70, soft_decline 0.60, abandonment 0.35, insufficient_funds 0.20, hard_decline 0.05, unknown 0.10. The module docstring is explicit: "documented, deliberately conservative prior — **not a measured fact**" (`config.py:1-7`), anchored to vendor claims (Stripe ~55% average recovery, Razorpay "up to 20%" for links, network resubmission caps, Baymard ~70% abandonment) (`config.py:8-18`). Ordering is test-asserted. Overridable per-merchant via custom `RevenueConfig` (`config.py:5-7`) — no merchant calibration exists in this deployment.
Failure classification feeding it: defensive normalized substring matching (no closed Razorpay enum), first-match-wins pattern table, `error_source` as weak fallback, UNKNOWN as last resort (`backend/app/services/revenue/classify.py:31-86,120-129`).

### 3.4 Stuck-checkout fallback — verified

- Builder: payments still `created` 30 min after creation → `stuck_checkout_payment` opportunities (order-level dedup: first-write wins, never double-counted) — `backend/app/services/recovery/builder.py:63-77,148-158`.
- Pricing: `opportunity_estimate` classifies the payment; when telemetry yields UNKNOWN (a stuck checkout has empty error telemetry), it falls back to the opportunity-type class default → `stuck_checkout_payment` maps to ABANDONMENT (0.35) instead of the UNKNOWN floor (0.10) — `engine.py:255-271`, `config.py:118-132`. Same mechanism: `dropped_checkout`→ABANDONMENT, `subscription_halted`→SOFT_DECLINE, `authorization_stuck`→TIMEOUT.

### 3.5 Environment scoping — verified consistent here

- Revenue reads ONLY the incident's own environment's payments (`source_types_for_environment`) for baseline, window, and returning-customer sets — `engine.py:141-148,371-403`.
- Opportunity builder is identically scoped — `builder.py:116-120`.
- Dashboard aggregates are environment-scoped, default `real_test` — `backend/app/api/v1/dashboard.py:5-7,147-153,202`.
- Contrast: diagnosis feature loading is the ONE unscoped reader (§2.2).

### 3.6 Refresh-on-view semantics

- `GET /api/v1/incidents/{id}` recomputes the counterfactual and overwrites `incidents.revenue_at_risk_paise` with `observed_loss.point_paise`, audited (`incident.revenue_at_risk_refreshed` with from/to/basis) — `backend/app/api/v1/incidents.py:272-274,345`, `backend/app/api/v1/dashboard.py:102-125`. A None point (zero baseline signal) leaves the stored (gross, detection-era) value untouched (`dashboard.py:106-109`).
- The service itself is read-only (`engine.py:15-16`).

---

## 4. Misleading terminology register

1. **`incidents.revenue_at_risk_paise` changes methodology over its lifetime.** At creation it is detection's GROSS affected-amount sum (`engine.py:883-902`); after the first detail view it is the counterfactual `observed_loss` point (`dashboard.py:102-125`). Same column, two definitions; the list view shows whichever is current. The swap is audited, but the field name never discloses which definition it currently holds — and with zero baseline signal the gross number silently persists.
2. **`observed_loss` is not observed.** It is a modeled counterfactual (what would have captured at baseline rates minus what did). `types.py:107-123` does call the four numbers distinct; the adjective still invites misreading as a measured loss. The only measured revenue numbers anywhere are `actual_recovered_paise` / `total_recovered_paise`.
3. **"recoverable" and "expected recovery" are prior-weighted planning numbers.** Honest in code (`config.py:1-7`: "not a measured fact") and always banded/flagged, but any UI surface that shows them without the `basis`/low_confidence context overstates them. (Not re-checked against the React components in this pass — the API does carry the flags: `incidents.py:317-341`.)
4. **"verified"** in recovered-revenue contexts means webhook-verified terminal status of a recovery action (`types.py:123,156-161`) — verification of the ACTION's outcome, not that PulseRecover caused the recovery. Causality is only addressed by the evaluation holdout's incremental lift (§3.1), which is harness-only.
5. **Diagnosis "91.5% fallback accuracy"** — see §2.4: true only on the linearly-separable toy generator; ~0.39–0.46 top-1 on realistic frames.
6. **"production" diagnosis accuracy headlines (0.910/0.995)** in docs/product-strategy.md:65 are exact-span-frame readings; the production-frame numbers on the same model are 0.7154/0.9308 (`diagnosis_active.json`). Frame choice changes the headline by ~20 points.

---

## 5. Findings summary (severity-tagged)

1. **[HIGH] Detection is on-demand only; nothing schedules it.** `POST /api/v1/detection/run` or the research-only demo trigger are the sole invocations; the worker runs retries/notifications/reconciliation only (`worker.py:1-27`; grep for "detection" in `backend/app/services/worker` = no matches). On the live deployment (APP_ENV=prod) the endpoint also requires the API key (`main.py:43-44,135`). Net: no autonomous detection exists on real_test.
2. **[HIGH] The real_test event stream is structurally starved.** `payment_events` rows are written ONLY by webhook intake (`webhook_handlers.py:298-309`) and the simulator; REST sync creates none (grep-verified). Detection and diagnosis features both consume this stream; with ~no real_test webhook events, a real_test detection pass returns "no terminal payment events in scope; nothing to detect" (`engine.py:280-284`). Flows D and E are exercised end-to-end only on simulator (research) data.
3. **[MEDIUM] Diagnosis feature computation is not environment-scoped.** `load_window_records` (`features.py:144-149`) filters only by time — the one cross-environment reader in an otherwise cleanly scoped system (detection/revenue/builder/dashboard all scope). Latent today (real_test has ~no events), but a same-clock-window overlap would silently contaminate features.
4. **[MEDIUM] The 91.5% heuristic-fallback claim does not generalize.** Honest on its stated toy dataset (ml.md:132; claim-matrix.md:143 "Attested"); 0.3922–0.4625 top-1 on realistic production frames (ml.md:320-321; exp02/exp05 metrics.json). Any audience reading it as the production fallback's accuracy is misled by ~2×.
5. **[MEDIUM] Shipped diagnosis model: production-frame top-1 0.7154 / macro-F1 0.6293** (active pointer), and it fails one stricter pre-registered continuity clause (span macro-F1 0.7664 < 0.7931 — disclosed at ship time, ml.md:454-458); bank_downtime exact-span F1 0.000 at support 15. Higher quoted numbers (0.878/0.993, 0.910/0.995) are different frames or the previous model.
6. **[LOW] `revenue_at_risk` terminology morphs** (register #1): gross sum → counterfactual on first view; the column name discloses neither.
7. **[LOW] Recoverability/effectiveness are priors anchored to vendor marketing claims** (Stripe/Razorpay/Baymard), never measured on this system; documented honestly in code, but they drive every rupee figure a merchant sees as "recoverable"/"expected".
8. **[LOW] Opt-in detection modes (same_time_yesterday, night_regime_floors) ship dark with disclosed-not-retuned values** (`engine.py:152-167`); published anchors were all measured with them OFF — enabling them changes admission behavior without re-anchored measurements.
9. **[INFO] LLM seam is dark**: `LLM_PROVIDER=none` live → all investigation reports are the deterministic heuristic reasoner (`config.py:38-42`, `reasoners.py:1117-1133`); the LLM path has never run against a real provider here.
10. **[INFO] Honest-by-design positives verified in code**: detection honesty markers (`meta.night_regime_floors`), heuristic flagging + confidence caps, zero-baseline → no point estimate, UNKNOWN recovery outcomes excluded, blocked policy proposals dropped from reports, audit rows on detection runs and revenue refreshes.

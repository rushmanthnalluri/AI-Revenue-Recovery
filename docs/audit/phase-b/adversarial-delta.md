# Phase B Adversarial Re-Review — Post-Phase-A Delta

Reviewer: adversarial re-reviewer (read-only). Scope: the delta `dcef95a..HEAD`
(`1b5487f` DEF-01/02/03 close-out, `3ecb851`, `ecd0181`, `e76cc12`, `64c5311`
symmetric webhook dedupe) plus the live burst tooling (`frontend/scripts/rz-pay-batch.mjs`).
Method: static attack on the new code paths; no test runs, no prod mutations.
Evidence classes as in the gate: code citations are `path:line`; anything I could
not derive from code is marked **UNCERTAIN**.

Pre-existing truths (per instructions) are NOT re-litigated: real sync works,
webhooks verified both directions, 300s worker detection, environment isolation,
+0.59pp CI-crosses-zero inconclusive evaluation, 993+9 green.

## Verdict summary

| # | Finding | Severity | Class | Disposition |
|---|---|---|---|---|
| F1 | Phantom capture-latency from sync-observation events poisons `capture_latency_ms` (series + baseline) | MEDIUM | EXISTING (latent, exposed by DEF-02 feed) | **BUILD NOW** |
| F2 | Stale webhook reprocess regresses a refunded payment to `captured` and marks the action RECOVERED; the new symmetric dedupe hides the regression from the event stream | MEDIUM (money-truth ceiling HIGH, likelihood LOW) | POSSIBLE | **BUILD NOW** |
| F3 | `64c5311`'s regression test does not pin the fix — it passes on the pre-fix code | LOW | EXISTING | **BUILD NOW** (test repair) |
| F4 | Worker detection pass vs manual `POST /api/v1/detection/run`: no DB uniqueness on incident signature → duplicate incidents under a real race | MEDIUM-low | POSSIBLE | BUILD LATER |
| F5 | Observation-time events vs gateway-time builder windows: incident revenue-at-risk with zero buildable opportunities; first-sync backfill can fire `insufficient_fund_share` on stale failures | LOW | EXISTING (by design, under-disclosed) | BUILD LATER / document |
| F6 | Both dedupe-guard skip paths are silent (no log, no audit, no metric) | LOW | EXISTING | BUILD NOW (fold into F2) |
| F7 | Stale embedded payment in a payment-link fetch can regress `Payment.status` inside one sync run (last-write-wins, no freshness check) | UNCERTAIN (trigger unverified) | SPECULATIVE | RESEARCH ONLY |
| F8 | Dedupe guard hiding a real later transition (created→failed with an existing sync `created` event) — **attacked, does not reproduce** | — | — | REJECT (non-finding) |
| F9 | Measured-outcome fit skewed by holdout composition — **attacked, does not reproduce** | — | — | REJECT (non-finding) |

No CRITICAL findings. The delta's dedupe design is sound for Razorpay's payment
lifecycle; the residual risk concentrates in **timestamp semantics** (F1/F5) and
**stale-event reprocessing** (F2/F3).

---

## F1 — Phantom capture-latency from sync-observation events (MEDIUM, BUILD NOW)

**Observation.** Sync-derived terminal events are stamped at *observation* time,
never gateway time — deliberately (`service.py:29-32,475-480`: only first-seen
`created` uses the gateway timestamp; first-seen-terminal uses `ingested_at`,
transitions use `utcnow()`). `_event_latency_ms`
(`app/services/detection/series.py:298-309`) computes latency for success events as
`event.occurred_at − (gateway_created_at or created_at)` with no check of
`event.source`. For a sync-observed capture that is exactly
*observation time − gateway creation time* — the poll lag, not a gateway latency.

**Attack / reproduction.** A payment created and captured at the gateway on day 0,
first synced on day 10, yields a sync `payment.captured` event whose computed
latency is ~864,000,000 ms. This is not a corner case — it is *every* sync-sourced
captured payment: on the gated prod account the entire real_test feed (6 payments →
6 events, all sync-derived, per phase-a-release-gate.md item A2) carries phantom
latencies today. Detection then buckets these into `capture_latency_ms`
(`engine.py:294,341-347`), where:
- `min_absolute_deviation` (75 ms, `engine.py:104`) is trivially cleared;
- `min_flagged_run=2` (schema default, `schemas/detection.py:31`) only *accidentally*
  defends the single-pass backfill (all observations share one `ingested_at` second
  → one bucket → run=1 → filtered); a merchant synced twice over ≥10 minutes with
  new captures each time flags two buckets and fires;
- the flagged backfill bucket persists and becomes a *decidable leading-baseline
  bucket* for later passes — true latency anomalies are then judged against a
  baseline poisoned with day-scale values (suppression of true positives).

**Current defense.** Only the incident floors, and only in the single-bucket case.
Nothing in the metric layer distinguishes observation-time events from
gateway-time events.

**Fix.** `_event_latency_ms` returns `None` for `event.source == 'sync'` (or:
require both endpoints gateway-stamped). Webhook-sourced captures keep their real
latency (`payload.created_at` is gateway event time, `webhook_handlers.py:262-266`).
An honest empty latency series for a poll-only merchant beats a fabricated one.
Small: one guard + two tests.

## F2 — Stale webhook reprocess regresses refunded→captured; dedupe hides it (MEDIUM, BUILD NOW)

**Observation.** `64c5311` made `_transition_payment` skip the event row when
`to_status` is already recorded, but deliberately *still advances payment state*
(`webhook_handlers.py:296-311`, commit message: "while still advancing payment
state"). Neither caller guards against moving state *backward*:
- `_handle_payment_captured` gates only on `payment.status != "captured"` (`webhook_handlers.py:162`);
- `_handle_payment_link_paid`'s embedded-payment path gates only on `payment.status != "captured"` (`webhook_handlers.py:204-206`). A payment in `refunded` passes both.

**Attack / reproduction.** (1) `payment.captured` arrives before any sync → payment
unknown → stored `processed=False` ("stored for reconciliation",
`webhook_handlers.py:160`). (2) Sync ingests the payment first-seen-captured →
observation event, status `captured`. (3) The payment is **refunded** at the
gateway (test mode supports refunds; refund webhooks are not subscribed — DEF-01
aligned the subscription to exactly `payment.captured/payment.failed/
payment_link.paid`, `render.yaml` per `1b5487f`). (4) Next sync observes
`refunded` → transition event, status `refunded`. (5) The reconcile sweep
(`reconcile.py:96-115`, every 900s and on every cold start — frequent on Render's
free tier) reprocesses the stored `payment.captured`: status `refunded != captured`
→ `_transition_payment` sets status back to `captured`, `payment.captured = True`,
and `_mark_action(..., RECOVERED)` fires for linked actions
(`webhook_handlers.py:166-168`). The new dedupe then *suppresses the event row* —
the event stream stays clean while the row regresses; pre-fix, the double-written
event at least made the regression visible. Net: recovered-revenue accounting can
be inflated by a refunded payment, with less forensic trace than before the fix.
Likelihood is LOW (the refund + observing sync must land inside the
≤15-minute sweep gap), consequence is money-truth corruption → MEDIUM.

**Current defense.** None on the state-advance path. The dedupe guard is neutral-to-negative
here (it removes the forensic event).

**Fix.** Terminal-ordering guard: never transition out of `refunded`/`captured`
into an earlier logical state from a reprocessed/stale event; the only legal
forward escape from `failed` is `captured` (late capture). Concretely: in both
callers, treat `refunded` as absorbing for `captured` transitions (3-line change +
2 tests). Optionally make `_transition_payment` refuse to move state when the
transition is already recorded AND the current status is terminal-success.

## F3 — The `64c5311` regression test passes on the pre-fix code (LOW, BUILD NOW)

**Observation.** `test_sync_recorded_transition_is_not_duplicated_by_webhook`
(`tests/razorpay/test_webhooks.py:531-564`) builds the payment with
`status="captured"` (line 536). With status already `captured`, the pre-existing
guard `if payment.status != "captured"` (`webhook_handlers.py:162`) short-circuits
*before* `_transition_payment` is ever called — the new dedupe branch is not
exercised. Verified against the diff: `64c5311` touches only `_transition_payment`
(+14 lines), so the handler-level guard pre-existed; this test is green with or
without the fix. The commit's stated bug (reconcile reprocess double-writing after
sync) is therefore **not pinned by any test**.

**Fix.** Change the fixture to `status="created"` with an existing
`to_status='captured'` sync event, then POST `payment.captured`: pre-fix writes a
second captured event (fails), post-fix writes none (passes). One-line change.
Side note (UNCERTAIN): I could not reconstruct the exact prod 4+4 sequence from
code alone — every straightforward ordering is already caught by the handler-level
status guards; the plausible trigger is a status/event-stream divergence (e.g.
F2's regression or F7's stale re-upsert). The corrected test pins the fix
regardless of which divergence produced it.

## F4 — Worker vs manual detection pass: duplicate-incident race (MEDIUM-low, BUILD LATER)

**Observation.** `run_detection` persists incidents by SELECT-then-INSERT in
Python (`engine.py:1025-1036`: candidates by `(metric, detection_method,
environment)` + meta fingerprint). `Incident` has **no unique constraint** on any
signature (`models/incidents.py:15-52`). The worker runs unit 4 in a thread
(`supervisor.py:69` via `asyncio.to_thread`; `worker.py:361-368`); the API endpoint
runs the same function in the request threadpool (`api/v1/detection.py:23-39`,
API-key-exempt outside prod so the console can trigger it). The window is
data-anchored (`engine.py:280-292`), so two concurrent passes over unchanged data
compute the *identical* window and both take the `match is None` branch → two
INSERTs → two OPEN incidents for one episode.

**Attack / reproduction.** Operator clicks "run detection" (or double-clicks)
while a 300s worker pass is in flight over the same real_test data → duplicate
incident cards; downstream, the opportunity builder runs per-incident
(`builder.py:107+`), so the same failed payments can be surfaced under two
incidents (per-incident dedupe `builder.py:122-127` does not dedupe *across*
incidents), inflating revenue-at-risk.

**Current defense.** Pass speed (sub-second at real_test volume) makes the window
milliseconds-wide; merge logic heals *subsequent* passes into one of the two rows
(candidates query has no ORDER BY — which twin absorbs future merges is
nondeterministic).

**Fix.** Postgres advisory lock around `run_detection`, or a deferred
unique/upsert on a persisted signature column. Not demo-blocking; schedule with
DEF-04/05-era hardening.

## F5 — Observation-time windows vs gateway-time builder windows (LOW, document / BUILD LATER)

**Observation.** Detection windows anchor on event `occurred_at`
(`engine.py:280-294`; `series.py:186-190`), which for sync-derived terminal events
is observation time. The opportunity builder selects failed payments by
`Payment.created_at` (`builder.py:251-253`), which for synced rows is the *gateway*
creation time (`normalize.py:139-140`). For promptly-observed payments (the live
burst: created→failed→observed within minutes) the two align. For stale
observations they diverge: a 10-day-old failure first observed "now" lands in the
current detection window, but its `created_at` falls outside it — the incident
reports revenue-at-risk while `build_for_incident` finds zero payments. Separately,
a first-sync backfill whose failures are ≥90% insufficient-fund in one 60-minute
bucket *can* fire `insufficient_fund_share`: that metric's floors are deliberately
minimal (`METRIC_MIN_FLAGGED_RUN=1`, `METRIC_MIN_FLAGGED_VOLUME=3`,
`engine.py:149-150`) and backfilled observations share one bucket. The success-rate
metric is protected (run=2 floor) in the single-pass case.

**Current defense.** Per-metric floors (partial); the builder mismatch is
self-limiting (no phantom opportunities) but produces an inconsistent dashboard
story (rupees at risk, empty recovery queue).

**Fix / accept.** Accept the semantics (observation-time is the honest stamp —
backdating would be fabrication, `service.py:30-32`); document the first-sync
behavior in docs/detection.md "Known limitations"; optional age-gate: exclude
first-seen-terminal observation events older than one bucket from anomaly
*scoring* while keeping them for baseline.

## F6 — Dedupe-guard fires are silent (LOW, BUILD NOW with F2)

Both skip paths — sync side (`service.py:462-471`) and webhook side
(`webhook_handlers.py:302-311`) — return without a log line, audit row, or
counter. When the guard hides something it shouldn't (F2), there is no signal;
when it works, its fire rate is unmeasurable. One `logger.info` per skip (payment
id, to_status, source already recorded) closes it. Fold into the F2 change.

## F7 — Stale embedded link payment regresses Payment.status (SPECULATIVE, RESEARCH ONLY)

`run_sync` pulls payments before payment links (`service.py:309-314`);
`_sync_payment_links` ingests the link's embedded `payments[]` through the same
`_ingest_payment` (`service.py:533-534`), and `_upsert` is last-write-wins with no
freshness comparison (`service.py:613-617`). **If** Razorpay's link-fetch returns a
stale embedded entity (e.g. `created` for a payment the payments pull already
recorded `failed`), the second ingest regresses `Payment.status`; the dedupe guard
then suppresses the regression event (to_status `created`/`failed` already
recorded), so the flap is invisible in the event stream and the builder's
`Payment.status == 'failed'` filter (`builder.py:251`) misses the payment on
regressed passes. Trigger is **UNCERTAIN** (depends on gateway behavior I cannot
probe read-only); the missing defense is certain. Research: capture one real
link-fetch payload for a terminally-resolved payment; if stale entities occur,
order the two pulls by freshness or skip embedded re-ingest when the local row is
already terminal.

## F8 — REJECTED: dedupe guard hides a real created→failed transition

The assigned attack does not reproduce. Both guards key on `to_status` only
(`service.py:464-468`, `webhook_handlers.py:304-309`): an existing sync
`payment.created` event has `to_status='created'`, which never matches a later
`failed` transition — the guard cannot skip it. Among the three subscribed events,
Razorpay's lifecycle admits no *second, distinct* transition to the same
`to_status` on one payment id (created→failed is terminal; failed→captured is the
one legal late move; captured→refunded has no subscribed event). The guard's only
real gap is F2's stale-advance, not a hidden transition.

## F9 — REJECTED: holdout composition cannot skew the outcome fit

The fit (`measure_outcomes`) runs on each arm's full scratch DB immediately after
`run_simulation`, *before* any recovery action or webhook (`runner.py:606-658`,
`outcomes.py:171-178`), and holdout assignment is an independent deterministic
function — customer-level `sha256('holdout:{seed}:{customer_id}')`
(`runner.py:458,737`). The fit never reads the assignment, so composition cannot
reach it. Composition does move the *estimate's* noise, but per-class rates +
fixed seed + per-run recorded config make that deterministic and reproducible;
the CI is published and brackets zero. Residual, already-symmetric biases
(accept, not games): the fit window includes the injected anomaly period in both
arms; dunning chains are right-censored at the simulation horizon in both arms;
the `payment_link`/`notify` columns remain anchoring assumptions, recorded
verbatim on every run (`outcomes.py:85-99,146-153`). Minor cosmetic:
`max(0.0, ...)` on late-capture lags (`outcomes.py:238`) silently clamps event-time
inversions instead of excluding them.

---

## Audit coverage of the new paths

- Worker detection pass: audited — `detection.run` row with `trigger:"worker"`
  per pass (`worker.py:74-91`); manual runs audited with environment
  (`api/v1/detection.py:43-66`). Covered.
- Sync-derived events: unaudited individually by design — the `sync_runs` row +
  event provenance (`source='sync'`, `derived_from` payload marker) is the ledger;
  only enable/disable is audited (`api/v1/merchant.py:97-108`). Acceptable; note
  the F6 silence gap.
- Webhook dedupe skip: nothing (F6). The `webhook_events` row records the
  delivery; the *skip decision* leaves no trace.
- Evaluation: assumptions + provenance stored per run (`outcomes.py:130-154`);
  honest-UI chip derives from stored CI (ecd0181). Covered.

## Candidate scoring (10-field contract, condensed)

### C1 — Source-gate `_event_latency_ms` (fixes F1) — **BUILD NOW**
- Observations: F1. Evidence: `series.py:298-309`, `service.py:472-480`, `engine.py:102-107`.
- Implementation concept: return `None` when `event.source == 'sync'` (latency
  unmeasurable from poll observations); keep payload `latency_ms` and
  webhook-sourced paths.
- Dependencies: none. Risks: real_test latency series goes sparse/empty for a
  webhook-quiet merchant — that is the honest state; document it.
- Test strategy: unit — sync-observed capture (old gateway_created_at, recent
  occurred_at) yields `latency_ms is None`; webhook capture keeps a real value;
  detection pass over a sync-fed account admits no latency anomaly.
- Demo value: prevents a fabricated day-scale latency number surfacing in a
  panel-visible incident during the real-merchant beat. Complexity: S.

### C2 — Terminal-ordering guard on stale transitions (fixes F2, F3, F6) — **BUILD NOW**
- Observations: F2/F3/F6. Evidence: `webhook_handlers.py:154-168,196-207,289-323`,
  `reconcile.py:96-115`, `tests/razorpay/test_webhooks.py:531-564`.
- Implementation concept: absorb `refunded` (and re-`captured`) against stale
  `captured` advances in both handlers; repair the F3 test to `status="created"`;
  add a skip-log line in both dedupe guards.
- Dependencies: none; independent of C1. Risks: a *legitimate* captured-after-refund
  edge does not exist in Razorpay's lifecycle (refund is post-capture terminal for
  the payment row), so the guard cannot suppress a real recovery.
- Test strategy: F2 repro (stored captured webhook → sync captured → sync refunded
  → reconcile reprocess → status stays `refunded`, action not RECOVERED, one
  skip-log); F3 corrected test fails on pre-fix code (verify by revert-check).
- Demo value: removes the one path where the console could claim RECOVERED on
  refunded money — the worst possible panel moment. Complexity: S.

### C3 — Detection-pass mutual exclusion (fixes F4) — **BUILD LATER**
- Evidence: `engine.py:1025-1036`, `models/incidents.py:15-52`, `supervisor.py:69`,
  `api/v1/detection.py:23-39`.
- Concept: pg advisory lock keyed on environment around `run_detection`, or a
  persisted signature + upsert. Dependencies: Postgres-only construct (SQLite dev
  path needs a no-op fallback). Risks: lock holder dying mid-pass (use
  session-scoped advisory locks). Test: two concurrent passes → one incident.
- Demo value: low (race is milliseconds-wide at current volume). Complexity: M.

### C4 — First-sync backfill semantics doc + optional age-gate (F5) — **BUILD LATER / document**
- Evidence: `builder.py:251-253`, `normalize.py:139-140`, `engine.py:149-150,280-294`.
- Concept: document observation-time semantics in docs/detection.md; optionally
  exclude first-seen-terminal observation events from scoring (keep for baseline).
- Demo value: explains the "incident with no buildable opportunities" state if a
  panelist syncs an old account live. Complexity: XS (doc) / M (age-gate).

### C5 — Stale embedded-link freshness probe (F7) — **RESEARCH ONLY**
- Evidence: `service.py:309-314,496-534,584-618`. Concept: one captured real
  link-fetch payload decides whether the defense is needed. Complexity: XS.

## Strongest recommendation

**C1 (F1) is the single strongest BUILD NOW**: it is live on prod *today* — every
sync-derived capture on the gated real account already carries a phantom
day-scale latency — it sits on the flagship real-merchant loop the demo walks
through, the fix is one guard plus two tests, and it converts a fabricated metric
into an honestly empty one. Pair it with C2 in the same PR-sized effort: C2's
ceiling (RECOVERED on refunded money, now without a forensic event) is the
highest-severity consequence in the delta even though its likelihood is low, and
its F3 test repair is one line.

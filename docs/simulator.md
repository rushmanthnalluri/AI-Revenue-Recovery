# PulseRecover Simulator

Deterministic synthetic payment environment for development, demo, and
scientific evaluation (ADR 0005). It writes realistic traffic into the shared
commerce tables (`merchants`, `customers`, `orders`, `payments`,
`payment_events`, `subscriptions`) and records exactly what it injected into
`simulator_runs` + `simulator_ground_truth` — the ground truth that detection,
diagnosis, and recovery are scored against.

**Scope of realism:** the simulator is modeled on documented Razorpay API
semantics + test-mode behaviors (`docs/research.md`, fetched 2026-08-26/27).
It uses **no proprietary Razorpay infrastructure, routing, issuer, or network
telemetry** — every distribution and failure rate in it is a synthetic,
disclosed choice (below), not a measured Razorpay statistic.

> Principle fit: the simulator plays the role of "payment infrastructure" and
> "reality". What it injects is the *answer key*; PulseRecover's agents must
> rediscover it from `payment_events` alone.

## Running it

From `backend/`:

```bash
python -m app.simulator --events 65000 --days 30 --seed 42   # full default
python scripts/seed.py --force                               # idempotent wrapper
python scripts/simulate.py --list                            # scenario presets
python scripts/simulate.py --scenario upi_outage_demo --force
```

Flags (`seed.py` / `python -m app.simulator`):

| flag | default | meaning |
|---|---|---|
| `--events` | 65000 | target `payment_events` count (a floor, see "Sizing") |
| `--days` | 30 | length of the simulated window |
| `--seed` | 42 | RNG seed; same seed + config ⇒ identical dataset |
| `--customers` | 3000 | customer count |
| `--incidents` | `default` | `default` \| `none` \| comma-separated kinds |
| `--scenario` | `standard` | label stored on the `simulator_runs` row |
| `--database-url` | app settings | override to seed a scratch/other DB |
| `--force` | off | delete the existing identical run and regenerate |

The window covers `[today 00:00 UTC − days, today 00:00 UTC)` unless
`SimulatorConfig.end_date` is set explicitly (tests do this for full
reproducibility).

**Idempotency.** The run id is deterministic: `sim_{seed}_{sha256(config)[:10]}`.
If a `completed` run with that id exists, seeding is a no-op (`skipped: true`
in the output). `--force` deletes the run and every commerce row it generated
(manual cascade in `engine.delete_simulator_run`, safe on SQLite and Postgres)
and regenerates. All entity ids derive from the run id, so two different runs
never collide.

**Verified performance** (2026-08-27, Windows, file-backed SQLite, WAL):
default config seeds in **~6.3–6.5s (~21k rows/sec)** → 67,727
`payment_events`, 30,491 payments, 29,763 orders, 3,000 customers, 150
subscriptions, 469 ground-truth rows, 131,602 rows total. Measured
`events_per_payment` = **2.22**.

## Determinism contract

One `random.Random(seed)` instance, consumed in a fixed code path:
customers → subscriptions (incl. dunning retries) → day-by-day checkout
payments. Same config always yields the same aggregate counts, the same entity
ids, and the same ground truth (`tests/simulator/test_determinism.py`
fingerprints the whole dataset). Entity ids deliberately do **not** use the
uuid4 helpers in `app.ids` — deterministic ids are what make reseeding
idempotent and ground-truth references stable.

## Data model mapping

- 1 merchant per run (`mch_{run_id}`).
- Customers: lognormal activity weights (a few heavy repeaters), per-customer
  reliability factor, preferred payment method (used ~70% of the time), ~2%
  `opted_out` (policy-engine input), city/acquisition metadata.
- Orders: one per checkout (and one per subscription cycle); `status`
  ∈ created/attempted/paid, `meta.attempts` counts payment attempts.
- Payments: gateway-style fields incl. `gateway_payment_id`, `method`,
  `status`, `error_code/source/description`, `meta.error_reason/error_step`,
  `meta.route` (pg_primary/pg_secondary), `meta.bank`, card network/type,
  UPI flow, wallet. `created_at` = simulated time (not seed wall time).
- Payment events: append-only Razorpay-shaped stream, `source="simulator"`:
  `payment.created` → (`payment.authorized` for card success) →
  `payment.captured` / `payment.failed`. Failure payloads carry the full
  `error_*` telemetry set; terminal capture payloads carry `latency_ms`.
- Subscriptions: monthly (₹299/₹499/₹999) and weekly (₹99) plans; cycle
  positions spread across the billing period; charges at 04:00–08:00 IST.
  Failed charges get Razorpay-style T+1/T+2/T+3 dunning retries (success
  55/50/45%); exhausted cycles move the sub to `halted` and it is never
  auto-charged again (per Razorpay docs).

### Modeled Razorpay quirks

- **`payment.failed` is not terminal**: ~1% of failures are followed by a late
  `payment.captured`; the payment row ends `captured` but keeps its error
  fields, and the event stream shows failed→captured.
- **Checkout abandonment**: payments stuck in `created` with exactly one
  event (natural background ~4%, plus injected spikes).
- **Checkout retries**: 15% of failed checkout payments get one customer
  retry (new payment row, same order, ~65% success).

## Distributions & assumptions

All assumptions are synthetic choices tuned to be plausible for an Indian
e-commerce merchant — they are **not** measured Razorpay statistics.

- **Method mix**: UPI 46%, card 33%, netbanking 13%, wallet 8%. Base success
  rates: card 0.86, UPI 0.84, netbanking 0.80, wallet 0.92 (× customer
  reliability, × 0.97 during 00:00–05:59 IST).
- **Amounts**: lognormal, median ₹499, σ=0.95, clipped to [₹100, ₹50,000],
  rounded to whole rupees. Netbanking ×1.4, wallet ×0.4.
- **Latency**: lognormal ms per method — card ~2.8s, UPI ~9.5s (collect
  flows), netbanking ~14s, wallet ~2.2s; clipped [300ms, 120s].
- **Seasonality (IST)**: hourly volume weights with lunch + late-evening
  peaks; day-of-week multipliers with weekend uplift; per-day lognormal
  jitter. Timestamps are stored tz-aware UTC (IST−5:30 conversion).
- **Subscriptions**: ~5% of customers; 12% baseline cycle failure
  (involuntary-churn-ish); ~3% cancel mid-window.

### Failure taxonomy

`app/simulator/taxonomy.py` mirrors Razorpay's documented
`error_code/error_source/error_step/error_reason` telemetry (see
docs/research.md, "Failure telemetry"). Razorpay's own enums are inconsistent
across docs pages, so the simulator fixes a defensible mapping:
`error_source ∈ {customer, bank, gateway}`, `error_step ∈
{payment_authentication, payment_authorization}`, `error_code` =
`BAD_REQUEST_ERROR` for customer/bank declines, `GATEWAY_ERROR` for
infrastructure. `upi_timeout` is kept as a distinct reason (Razorpay surfaces
it as UPI `payment_timed_out`) so diagnosis can separate UPI collect timeouts
from card 3DS timeouts. Each reason is flagged `recoverable` (soft vs hard
decline) — the recovery strategy layer should treat hard declines
(`card_number_invalid`, `pin_attempts_exceeded`, …) as no-retry.

## Incident taxonomy

Six parameterized kinds (`app/simulator/config.py::IncidentKind`), each a
window `[start, end)` plus modifiers applied to in-scope payments
(`app/simulator/incidents.py`):

| kind | target | effect | expected signature → root cause |
|---|---|---|---|
| `gateway_degradation` | all methods | +35% fail (gateway_technical_error), latency ×2.5 | `payment_success_rate` drop → `gateway_outage` |
| `method_outage` | one method (+optional bank) | +80% fail (bank_downtime/bank_technical_error) | SR drop on `{method}` → `bank_downtime` |
| `route_latency` | one route (pg_primary) | latency ×8, +6% fail (payment_timed_out) | `capture_latency_ms` spike on `{route}` → `gateway_route_latency` |
| `customer_insufficient_funds_wave` | card+UPI | +30% fail, forced insufficient_fund (90%) | SR drop, `error_reason=insufficient_fund` → `insufficient_balance_pattern` |
| `checkout_abandonment_spike` | non-subscription | +45% abandon (payment stays `created`) | `checkout_abandonment_rate` spike → `checkout_abandonment` |
| `subscription_failure_spike` | subscription charges | +55% fail (soft-decline mix), subs may halt | `subscription_failure_rate` spike → `subscription_soft_declines` |

The default 30-day schedule places one incident of each kind at
`day_fraction` 0.18/0.35/0.55/0.70/0.82/0.90 (fractions scale with `--days`).
Only payments the incident actually changed (outcome flipped, abandonment
forced, or latency multiplied) are counted as *affected* — in-window payments
left untouched are not labeled.

## Ground truth → label mapping

`simulator_ground_truth` rows (unique per `(run, entity_type, entity_id)`):

1. **`entity_type="incident"`** — one per injected window. `truth`:
   `kind`, `start`/`end` (ISO UTC), `params`, `affected_payment_ids` (full
   list) + `affected_count`, `injected_failures`, `injected_abandonments`,
   `latency_affected`, `affected_amount_paise`, plus the *expected* labels:
   `expected_root_cause` (diagnosis scoring key — vocabulary matches
   `diagnoses.predicted_cause`), `expected_signature` (metric + dimension +
   direction, detection scoring key), `recoverable`, `recovery_hint`.
2. **`entity_type="payment"`** — one per affected payment. `truth`:
   `incident_ids`, `natural_outcome` vs `final_outcome` (would-have-been vs
   actual terminal state; `injected` = they differ), `error_reason`,
   `is_subscription`, `recoverable`. Note: an incident-flipped payment that
   then *late-captures* shows `injected=false` (natural==final==captured) —
   use membership in the incident's `affected_payment_ids` as the label.
3. **`entity_type="subscription"`** — subs that ended `halted` after the
   subscription spike contributed to their failures: `halted: true`,
   `incident_ids`, `expected_recovery`.

The simulator never writes to `incidents` — that table is filled by the
detection pipeline, and evaluation joins it against this ground truth.

## Scenario presets

`app.simulator.config.SCENARIOS` (surfaced later via `/api/v1/demo`):
`standard` (default), `quiet` (no incidents — baseline), `upi_outage_demo`,
`payday_wave_demo`, `storm` (8 incidents, overlaps).

## Tests

`backend/tests/simulator/` — determinism (full-dataset fingerprint), default
volume bar (≥60k events), ground-truth/internal consistency, per-kind incident
metric shifts (in-window vs out-of-window), taxonomy conformance, seed
idempotency/force/delete. Run: `pytest tests/simulator -v`.

# Data Provenance

Where every byte of data in PulseRecover comes from, classified honestly.
This document is the companion to the `source_type` / `source_system` /
`external_id` / `ingested_at` columns on the commerce tables (see
"Provenance schema" below) — the columns tag each row; this file explains
what the tags mean and audits every data source in the repo.

## Classification vocabulary

| Class | Meaning |
|---|---|
| `REAL_API` | Data returned by the real Razorpay REST API (`api.razorpay.com`, Test Mode or Live) over the network. |
| `REAL_WEBHOOK` | Data delivered by Razorpay to `POST /webhooks/razorpay` with a valid `X-Razorpay-Signature` against the configured webhook secret. |
| `REAL_DATABASE` | Durable application state in the configured `DATABASE_URL` (the system of record). |
| `SYNTHETIC_SIMULATOR` | Deterministically generated synthetic data from the PulseRecover simulator (engine or simulated gateway). Never real money, never a real customer. |
| `STATIC_FIXTURE` | Checked-in or generated-once files used as fixed inputs/outputs (test factories, sample artifacts, experiment specs). |
| `MOCK` | Test doubles standing in for an external system at a protocol seam (e.g. `httpx.MockTransport`). |
| `HARDCODED` | Constants embedded in source code (model parameters, defaults). Not data records, but they shape generated data. |

## The one-sentence truth

**Today every row in the commerce tables (`merchants`, `customers`, `orders`,
`payments`, `payment_events`, `subscriptions`) is `SYNTHETIC_SIMULATOR`.**
The simulator engine is the only code path that creates those rows; real
Razorpay data can only *update* an existing payment (via webhook) or live on
`recovery_actions.gateway_response`. There is no ingestion path that pulls
real Razorpay payments/orders/customers into the database yet.

## Startup behavior — verified claim

**The app never auto-seeds on startup.** Verified against the code:

- `backend/app/main.py` (`create_app()`): no lifespan/startup handler, no
  seeding call — it only wires middleware, routers and exception handlers.
- `backend/app/db.py`: creates the engine and session factory only; no
  `create_all`, no row writes.
- `Base.metadata.create_all` appears only in `backend/app/simulator/cli.py`
  (`make_session`, table creation only — zero rows) and in test fixtures.

Seeding is always an explicit operator action. Every entry point:

| Entry point | What it does | Class of data written |
|---|---|---|
| `POST /api/v1/demo/scenario/{name}` (`backend/app/api/v1/demo.py`) | Idempotent simulator run + one anchored detection pass | SYNTHETIC_SIMULATOR |
| `POST /api/v1/demo/reset` (`backend/app/api/v1/demo.py`) | Deletes simulator commerce rows + derived tables (keeps evaluation_runs/experiments/model_predictions/audit_logs) | — |
| `python scripts/seed.py` (`backend/scripts/seed.py` → `app/simulator/cli.py`) | CLI simulator seed, idempotent by deterministic run id | SYNTHETIC_SIMULATOR |
| `python scripts/simulate.py` / `python -m app.simulator` (`backend/app/simulator/cli.py`, `__main__.py`) | Named scenario presets into `DATABASE_URL` | SYNTHETIC_SIMULATOR |
| `python scripts/demo_run.py` (`backend/scripts/demo_run.py`) | Five demo scenarios against a scratch sqlite file, never `DATABASE_URL` | SYNTHETIC_SIMULATOR |
| `python scripts/demo_live.py` (`backend/scripts/demo_live.py`) | Live-demo beats against the deployed stack (signed sim-gateway webhooks) | SYNTHETIC_SIMULATOR |
| `frontend/e2e/global-setup.ts` | Seeds the scratch e2e stack via `POST /api/v1/demo/scenario/upi_outage_demo` | SYNTHETIC_SIMULATOR |
| `backend/scripts/run_evaluation.py`, `run_multi_anchor.py`, `agent_eval.py`, `train_models.py` | Evaluation/training harnesses; run the simulator into scratch DBs | SYNTHETIC_SIMULATOR |

## Data source audit

### Application runtime (`backend/app/`)

| Source | Path | Class | Notes |
|---|---|---|---|
| Simulator engine (writes merchants/customers/orders/payments/payment_events/subscriptions + ground truth) | `backend/app/simulator/engine.py` | SYNTHETIC_SIMULATOR | The **only** writer of commerce rows. Deterministic ids (`pay_<run>_<seq>`, gateway ids `pay_S<seed>_...`); fake customers `user{i}s{seed}@example.com` / `Sim User {i}` / `+919...`; demo merchant `mch_<run_id>` with `ops@pulserecover-demo.example.com`, `acc_sim<seed>`. |
| Simulator taxonomy & distributions | `backend/app/simulator/taxonomy.py`, `distributions.py`, `incidents.py`, `config.py` | HARDCODED | Model parameters (success rates, method mix, banks, failure catalog, incident effects) that shape the synthetic data. |
| Simulated gateway (in-memory `PaymentGateway` twin) | `backend/app/services/razorpay/simulated.py` | SYNTHETIC_SIMULATOR | No DB writes, no network. Deterministic `order_sim*`/`pay_sim*`/`plink_sim*`/`sub_sim*`/`evt_sim*` ids; `DEFAULT_WEBHOOK_SECRET = "sim-webhook-secret"` and `DEFAULT_BASE_TS` are HARDCODED. Labels itself "SIMULATION ONLY" in the module docstring. |
| Real Razorpay adapter | `backend/app/services/razorpay/client.py` | REAL_API | Raw REST over httpx. Responses are stored only on `recovery_actions.gateway_response` and used for GET-only re-queries (`executor.resolve`, `reconcile`). Writes **no** commerce rows. |
| Gateway factory | `backend/app/services/razorpay/factory.py` | — | Selection seam: `SIMULATION_MODE=true` or missing keys → simulated gateway; else real adapter. No data. |
| Webhook ingress (writes `webhook_events`) | `backend/app/api/v1/webhooks.py` | REAL_WEBHOOK when the real gateway verifies the signature; SYNTHETIC_SIMULATOR when the sim gateway does | `webhook_events.source` is already stamped `"simulator"` or `"razorpay"` at intake. |
| Webhook handlers (append `payment_events`, transition `payments`) | `backend/app/services/recovery/webhook_handlers.py` | REAL_WEBHOOK / SYNTHETIC_SIMULATOR | Only *updates* existing payments found by `gateway_payment_id` and appends status events (`source="webhook"`). Cannot create payments. Provenance of these event rows is tagged by the provenance schema (below). |
| Recovery executor | `backend/app/services/recovery/executor.py` | REAL_API (when real mode) | Gateway mutations (create order/payment link); responses stored on the action row. No commerce-row writes. |
| Reconciliation sweep | `backend/app/services/recovery/reconcile.py` | — | Replays stored webhook events through the same handlers + GET-only re-queries. Provenance inherits from the original event. |
| Detection / diagnosis / recovery builder / policy / agent | `backend/app/services/detection/`, `diagnosis/`, `recovery/builder.py`, `policy/`, `agent/` | REAL_DATABASE (derived) | Read commerce rows, write derived tables (incidents, diagnoses, opportunities, strategies, actions, audit logs). Derived truth, not new source data. |
| Local dev databases | `backend/pulserecover.db`, `backend/e2e_test.db` | REAL_DATABASE containing SYNTHETIC_SIMULATOR rows | Scratch/dev stores; contents seeded only via the entry points above. |

### Tests (`backend/tests/`)

| Source | Path | Class | Notes |
|---|---|---|---|
| ORM factories (`make_merchant`, `make_payment`, `make_incident`) | `backend/tests/conftest.py` | STATIC_FIXTURE | Test-row factories against in-memory SQLite. |
| Canned Razorpay API responses | `backend/tests/razorpay/test_client.py` | MOCK | `httpx.MockTransport` handlers returning fixed payloads — no network. |
| Sim-gateway dependency override + `WH_SECRET` | `backend/tests/razorpay/conftest.py` | MOCK / STATIC_FIXTURE | Lets webhook tests produce genuinely valid HMAC signatures. |
| Integration fixtures (`SimulatedPaymentGateway(success_rate=1.0)`) | `backend/tests/integration/conftest.py` | MOCK | In-memory DB + sim gateway seams. |
| Simulator test configs (`small_config`, `FIXED_END`) | `backend/tests/simulator/conftest.py` | STATIC_FIXTURE | Fixed seed/window for reproducible simulator assertions. |
| Provenance tests | `backend/tests/provenance/test_provenance.py` | — | Verify the provenance schema on both write paths + migration upgrade/downgrade. |

### Frontend (`frontend/`)

| Source | Path | Class | Notes |
|---|---|---|---|
| API client | `frontend/src/lib/api.ts` | REAL_DATABASE (via API) | All views fetch from the backend API. **No mock/fixture/fake data in the UI** — the only "fake" mention is a comment in `components/investigation/investigation-types.ts:213` stating an error is shown *instead of* fake data. |
| E2E seed state | `frontend/e2e/seed-state.ts`, `.tmp/seed-state.json` | STATIC_FIXTURE | Pointer file (ids of seeded entities) written by `global-setup.ts`. |
| E2E stack config | `frontend/e2e/stack.ts` | HARDCODED | Scratch DB URL / ports / API key for the e2e environment. |

### Artifacts & ML (`backend/artifacts/`, `ml/`)

| Source | Path | Class | Notes |
|---|---|---|---|
| Trained diagnosis models | `backend/artifacts/diagnosis_*.joblib` | STATIC_FIXTURE (derived) | Trained on simulator-derived frames. |
| Training/eval frames | `backend/artifacts/*.csv` (`diagnosis_train_frame`, `prod_frames_*`, `tight_frames_*`, `aug_pure_sr_v1`, `sim_features`) | STATIC_FIXTURE (derived from SYNTHETIC_SIMULATOR) | Feature frames extracted from simulator runs. |
| Eval outputs | `backend/artifacts/eval_*.json`, `calib*.log`, `eval_after.err` etc. | STATIC_FIXTURE (derived) | Recorded evaluation runs. |
| Scratch DBs | `backend/artifacts/calib.db`, `eval_detection.db`, `insights_sample.db`, `stuck_checkout_verify.db` | STATIC_FIXTURE (simulator-seeded) | Point-in-time scratch databases. |
| Insights sample | `backend/artifacts/insights_sample.py`, `insights_sample.json` | STATIC_FIXTURE | Canned insights output sample. |
| Experiment specs/outputs | `ml/experiments/**` (`canonical_spec.json`, `detection/`, `diagnosis/`, `agent/`, `multi_anchor/`) | STATIC_FIXTURE | Experiment definitions and recorded results. |
| Policy config | `policies/default.yaml` | HARDCODED config | Deterministic policy rules (amount ceilings, confidence floors). |

## Provenance schema (migration `f3a9c1e7b204`)

Six commerce tables (`merchants`, `customers`, `orders`, `payments`,
`payment_events`, `subscriptions`) carry four provenance columns:

| Column | Type | Default | Meaning |
|---|---|---|---|
| `source_type` | `VARCHAR(32) NOT NULL` | `'simulator'` (server default) | `simulator` \| `razorpay_test` \| `razorpay_live`. The server default honestly tags every pre-existing/backfilled row as simulator output. |
| `source_system` | `VARCHAR(64) NULL` | — | `pulserecover-simulator` \| `razorpay`. |
| `external_id` | `VARCHAR(64) NULL` | — | Upstream id: Razorpay `pay_`/`order_`/`sub_`/`cust_` id, or the simulator's deterministic gateway id. |
| `ingested_at` | `TZDateTime NOT NULL` | `utcnow` (ORM default) | When the row entered this database (wall clock; simulator `created_at` stays in the simulated window). |

Writer tagging (enforced by `backend/tests/provenance/test_provenance.py`):

- Simulator engine rows → `source_type='simulator'`,
  `source_system='pulserecover-simulator'`, `external_id` = the row's
  deterministic gateway id (`gateway_payment_id` etc.).
- Webhook-appended `payment_events` → `razorpay_test`/`razorpay` when the
  configured gateway is the real adapter, `simulator`/`pulserecover-simulator`
  when it is the simulated gateway (`factory.use_simulator`); `external_id` =
  the Razorpay payment id from the payload.
- Dedup: `payments` has `UNIQUE (source_type, external_id)` so a real
  ingestion path cannot double-store the same Razorpay payment. Existing
  per-column uniques (`gateway_payment_id`, `gateway_order_id`,
  `gateway_subscription_id`) are unchanged; simulator idempotency
  (deterministic run id + delete-before-reseed) is unaffected.

Backfill semantics (existing rows, all of which predate real ingestion):

- `source_type = 'simulator'` for every existing row (server default).
- `source_system = 'pulserecover-simulator'` for the five single-writer
  tables (the engine is provably the only writer); `NULL` for
  `payment_events`, which has mixed writers (`source` column keeps
  `simulator`/`webhook`/`poller`/`seed` detail).
- `external_id` = the existing `gateway_*` id where one exists; `NULL`
  otherwise (`payment_events` has no upstream id column).
- `ingested_at` = migration time (honest: the true ingestion time of legacy
  rows was not recorded).

## Environment model (migration `b4e7a1c2d305`)

The strict boundary between **REAL MERCHANT mode** (Razorpay Test Mode data)
and **RESEARCH mode** (simulator data). Every read surface and every writer
respects exactly one environment per query/pass/action; a research row can
never surface through a real_test query.

### Vocabulary and mapping

| Environment | Meaning | Commerce `source_type` set |
|---|---|---|
| `real_test` | REAL MERCHANT mode — data from Razorpay Test Mode (API or webhook) | `razorpay_test`, `razorpay_live` |
| `research` | RESEARCH mode — simulator output (engine + simulated gateway) | `simulator` |

`app.models.base.source_types_for_environment` is the only sanctioned mapping
between the two provenance axes. Commerce tables carry **no** environment
column — their environment is *derived* from `source_type`. Derived tables
carry the column directly.

### The `environment` column

`environment VARCHAR(16) NOT NULL DEFAULT 'research'` (indexed) on:
`incidents`, `incident_evidence`, `recovery_opportunities`,
`recovery_actions`, `diagnoses`, `agent_reports`, `audit_logs`.

The `'research'` default is the safe failure direction: a writer that forgets
to stamp lands in the research sandbox and can never leak into a real_test
query. Every pre-existing row is simulator-derived, so the backfill is honest.

Writer stamping chain (enforced by `backend/tests/environment/`):

- Detection runs take `environment` on `DetectionRunRequest` (default
  `real_test`); the pass scores only payments whose `source_type` belongs to
  that environment and stamps incidents + evidence. Dedup/merge/suppression
  candidates are environment-scoped, so the same signature can exist once per
  environment.
- The opportunity builder stamps opportunities with the incident's
  environment and scopes candidate payments/orders to it.
- The recovery executor stamps actions with the opportunity's environment.
- Simulator callers (demo router, evaluation harness, demo scripts) pass
  `environment='research'` explicitly; real ingestion (a later wave's sync
  service + real webhooks) produces `real_test` rows.

### Gateway-by-environment execution

`RecoveryExecutor` routes gateway calls by the opportunity's stamp:
`research` → the injected gateway (the simulated twin in every current
deployment); `real_test` → the REAL Razorpay adapter (`real_gateway` seam for
tests, else the configured adapter). If real keys are absent the executor
refuses honestly — `GatewayNotConfiguredError` → HTTP 409
`razorpay_not_configured` — never a fake execution, never the simulator.

### Sync-service tables (contract for the ingestion wave)

- `sync_runs` (`sr_`-prefixed ids): one row per real-ingestion sync pass —
  `started_at`, `finished_at`, `status` (`running`/`completed`/`failed`),
  `entity_counts` JSON, `error`, `actor`, `request_id`, `created_at`.
- `connection_state` (singleton id `'merchant'`): `sync_enabled`,
  `last_sync_at`, `last_webhook_at`, `last_sync_status`, `updated_at`.

### Read-API scoping

`GET /api/v1/dashboard/summary` + `/timeseries`, `GET /api/v1/incidents`,
`GET /api/v1/recovery/opportunities`, `GET /api/v1/audit`, and
`GET /api/v1/payments` take an `environment` query param **defaulting to
`real_test`** (the merchant-facing mode; documented behavior change). Detail
endpoints (`/incidents/{id}`, `/recovery/{id}`, `/recovery/{id}/plan`) stay
addressed by id and follow the row's own environment. `POST /api/v1/demo/reset`
deletes only simulator-sourced commerce rows and research-environment derived
rows — real_test rows are untouchable — and its own audit row is
research-tagged.

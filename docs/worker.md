# PulseRecover — In-Process Worker (scheduler tier)

The P2 worker tier deferred by ADR 0009/0011, landed as a dependency-free
asyncio loop inside the API process. No APScheduler, no Celery, no extra
infra: one supervisor paces a synchronous `Worker` on a ~30s tick.

Owner: recovery execution engineer. Code: `backend/app/services/worker/`,
executor scheduling in `backend/app/services/recovery/executor.py`.
Tests: `backend/tests/worker/`.

> The worker changes WHO fires due work, never WHAT decides: every execution
> still passes the deterministic policy gate, every transition still lands in
> `audit_logs`, and exactly one gateway mutation per action still holds.

## 1. What the worker does each tick

`Worker.tick()` runs three failure-isolated units (one bad unit never skips
the others; a failing unit retries next tick):

| unit | source of truth | what it does |
|---|---|---|
| Delayed retries | `recovery_actions.status == SCHEDULED` | fires due actions through `RecoveryExecutor.execute` — same re-gate, same guards |
| Notification outbox | `notification_outbox` rows `PENDING` and `due_at <= now` | delivers via the `NotificationSender` port; backoff retry, then `FAILED` |
| Reconciliation | ADR 0011 sweep (`run_reconciliation`, unchanged) | runs on the first tick after startup, then every `WORKER_RECONCILE_SECONDS` (default 15 min) |

Each unit commits per repaired row (mirroring the sweep's documented
per-unit commits), so one bad row never undoes earlier work in the tick.
The worker is single-process by design — the deployment is a one-node
monolith; the executor's opportunity row lock remains the cross-writer
guard on Postgres. Actor on every worker-written audit row and policy
re-decision: `system:worker`.

## 2. Configuration (`app/config.py`)

| setting | default | meaning |
|---|---|---|
| `WORKER_ENABLED` | `false` | master switch. OFF by default: the test suite, the evaluation harness, and one-shot scripts never spawn the loop. The app lifespan (`app/main.py`) starts/stops the supervisor only when true. |
| `WORKER_TICK_SECONDS` | `30.0` | delay between ticks (shutdown is prompt — the loop sleeps on an event, not a bare sleep). |
| `WORKER_RECONCILE_SECONDS` | `900.0` | reconciliation cadence. The first tick after startup always sweeps (drift accumulates while the process is down). |
| `WORKER_NOTIFICATION_SENDER` | `logging` | sender selection: `logging` (simulated default) or `razorpay_notes` (real-environment seam, §4). |

## 3. Delayed retries — SCHEDULED

A strategy requesting `constraints.delay_seconds` no longer fires at once:
when the gate ALLOWs it, the executor parks the action in the new
`RecoveryStatus.SCHEDULED` state (added to `OPEN_STATES` and
`CANCELLABLE_STATES` — it holds the opportunity's execution slot and stays
cancellable; nothing has reached the gateway, no attempt is consumed).

- **Due time** = latest policy decision (`decided_at`, falling back to
  `proposed_at`) + `delay_seconds`. No new column: the schedule is a pure
  function of the audited row. Re-gating restarts the wait honestly.
- **Firing**: each tick, the worker scans parked actions and calls
  `execute()` on the due ones. `execute()` re-gates at fire time (duplicate
  protection, stopping rules, rate limits re-checked fresh) and only then
  fires — BLOCKED now means REJECTED, REQUIRES_APPROVAL moves the action to
  the human lane. A manual `execute()` on a parked action is an idempotent
  no-op while it is not due.
- **Exactly-once**: firing leaves SCHEDULED, so later ticks never re-scan
  the action; the mutation reuses the `gateway_request_id` minted at
  proposal time; the opportunity row lock serializes the worker against
  concurrent API callers. Proven by
  `tests/worker/test_delayed_retries.py::TestWorkerFiresWhenDue`.
- The requested delay is still recorded in the gateway order's notes
  (`requested_delay_seconds`) as part of the audited proposal.

## 4. Notification outbox

A `notify_customer` fire enqueues one `notification_outbox` row (customer,
channel from strategy constraints, payload, `PENDING`, `due_at = now`,
environment stamped from the action) and references it from the action's
`gateway_response.outbox_id`. The action's verification is unchanged:
`VERIFYING` until the customer's payment webhook lands.

Delivery per tick: rows `PENDING` and past `due_at` go through the
`NotificationSender` port (`app/ports.py`):

- `LoggingNotificationSender` (default) — simulated delivery: the
  notification is logged; the row is marked `SENT` with
  `delivered_via="logging"`. Safe everywhere.
- `RazorpayNotesNotificationSender` (`WORKER_NOTIFICATION_SENDER=razorpay_notes`)
  — the real-environment SEAM. Razorpay exposes no standalone
  customer-notification API in the `PaymentGateway` port, so this sender
  performs no external delivery today; its receipt carries
  `simulated: true` so the provenance stays honest. A live SMS/email
  provider integrates by implementing `NotificationSender` — the outbox and
  worker do not change.

Failure handling: a raising sender leaves the row `PENDING`, bumps
`attempts`, and pushes `due_at` out by linear backoff
(`NOTIFICATION_RETRY_BASE_SECONDS` × attempts, base 60s); at
`NOTIFICATION_MAX_ATTEMPTS` (3) the row is `FAILED` — surfaced, never
silently dropped. Enqueue, delivery, retry, and failure each write an
`audit_logs` row (`notification.queued|sent|retry_scheduled|failed`) with
the environment stamped.

## 5. Liveness

`GET /api/v1/system/health` includes a `worker` check:

- `disabled` when `WORKER_ENABLED=false` (deliberate configuration — the
  aggregate status stays `ok`);
- `ok` with the last tick age when the loop is alive;
- `degraded` when the last tick is stale (> max(2 × tick, 10s));
- `down` when enabled but no tick has been recorded.

The supervisor stamps `last_tick_at` when a tick BEGINS (liveness = the
loop is alive); a failing tick body is recorded in `last_error` and echoed
in the check detail.

## 6. What the worker deliberately does NOT change

- The ADR 0011 sweep endpoint (`POST /api/v1/recovery/reconcile`) stays
  operator-triggerable; the worker reuses the same service as-is.
- `services flush, the API layer commits`: the worker owns its own
  short-lived sessions and commits per repaired row — the same documented
  exception shape as the sweep.
- The two data environments never mix: firing routes by the action's own
  environment stamp through `RecoveryExecutor._gateway_for` (research →
  simulator twin, real_test → real Razorpay or an honest
  `razorpay_not_configured` refusal, retried next tick); outbox rows carry
  the action's environment for provenance.

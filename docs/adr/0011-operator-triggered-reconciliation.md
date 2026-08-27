# ADR 0011: Operator-triggered reconciliation sweep

- **Decision:** A single idempotent sweep — `POST /api/v1/recovery/reconcile`
  backed by `app.services.recovery.reconcile` — repairs both drifts the closed
  loop can leave behind: UNKNOWN `recovery_actions` are resolved by GET-only
  gateway re-query, and `webhook_events` with `processed=false` are re-run
  through the same handler registry as live intake. No background scheduler;
  an operator (or the demo script) triggers it.
- **Context:** Two truths the system already accepted but never repaired:
  (1) an action whose gateway mutation was sent exactly once with an ambiguous
  outcome lands in UNKNOWN — resolution required a human to re-hit execute
  (which delegates to `executor.resolve`) one opportunity at a time; (2) a
  webhook handler that failed at intake (e.g. the event arrived before the
  payment row existed) was stored with `processed=false` — deliberately, so
  dedup cannot swallow it — but nothing ever looked at those rows again. The
  monolith has no worker tier: ADR 0009 already deferred background jobs for
  the evaluation harness, and the same constraint applies here. The webhook
  handler registry also lived in the API layer, which made reuse by anything
  but the intake endpoint a layering violation (the evaluation harness
  already imports it from there — sanctioned, see ADR 0010).
- **Options:**
  1. Operator-triggered synchronous sweep endpoint (chosen).
  2. Background scheduler/worker tier (cron in-process, or Celery/RQ + Redis)
     re-running the sweep unattended.
  3. Repair only at the existing touchpoints (manual re-execute for UNKNOWN;
     webhooks never reprocessed) — the status quo.
- **Chosen:** (1), with the handler registry moved to
  `app.services.recovery.webhook_handlers` so intake, the sweep, and the
  evaluation harness share one verification code path; `api/v1/webhooks.py`
  remains the thin ingress adapter (HMAC gate, event-id dedup, raw-event
  persistence, fast ack).
- **Why:** Correctness comes from reuse, not new machinery: UNKNOWN resolution
  calls `RecoveryExecutor.resolve` (GETs only — `fetch_order`/`fetch_payment`;
  RECOVERED only on positive gateway evidence, never a blind retry), and
  failed events go through the identical `dispatch_event` registry function
  the endpoint uses, so a reprocessed event behaves bit-for-bit like a live
  one. The sweep commits per repaired unit — a documented exception to
  "services never commit" — because `dispatch_event` rolls the whole session
  back on handler failure, and batching would let one bad event silently undo
  earlier repairs; per-unit commits also make the sweep safely re-runnable
  mid-failure. A synchronous operator trigger keeps the demo deterministic
  and controllable (same argument as ADR 0009) and needs zero new
  infrastructure. Every sweep writes one `recovery.reconcile` audit row with
  the full report; a second sweep over a clean database is a no-op beyond
  that row.
- **Tradeoffs:** Reconciliation latency is operator-paced, not
  scheduler-paced — an unattended UNKNOWN stays surfaced-but-open until
  someone triggers the sweep (acceptable for the demo; the API makes it one
  curl). The sweep holds the HTTP request while it works; at large backlogs
  it would need pagination or the worker tier. That tier — scheduled
  reconciliation, async notifications, delayed retries — remains deferred to
  P2 (see `docs/product-strategy.md`), exactly as ADR 0009 deferred it for
  evaluation; when it lands, this service is the ready-made unit of work it
  should call.

# PulseRecover — Data Flow

**Companion to:** `docs/architecture.md` (components), `docs/security-architecture.md` (trust boundaries).
**Conventions:** money = integer paise + `currency:"INR"`; ids are prefixed (`inc_`, `opp_`, `act_`, `pol_`, `aud_`, `whk_`, `gwr_`, `agt_`, `run_`); every request carries `X-Request-ID` (middleware) which lands on audit rows; every state change writes `audit_logs` via `app.services.policy.audit` (flush-only — rides the caller's transaction); all datetimes tz-aware UTC.

## 1. Detection pass

```
POST /api/v1/detection/run {window, segment, detector, dry_run}
  → detection.engine: bucket payment_events (latest terminal event wins — failed→captured is honored)
     into 4 metric series: payment_success_rate, capture_latency_ms,
     checkout_abandonment_rate (attempt-based; right-censoring handled honestly),
     insufficient_fund_share (failure-mix signal for thin windows)
  → detectors (zscore | ewma | cusum | isolation_forest) vs rolling baseline
  → segment localization (method column + bank/gateway/route from payment meta)
  → UPSERT incidents (key: metric+detector+window+segment; re-runs UPDATE, never duplicate;
     human-triaged status never clobbered)
  → incident_evidence rows: metric_series snapshot + segment_breakdown
  → audit_logs: detection.pass / incident.detected
```
Dry-run computes everything, persists nothing. Detection latency = `detected_at` − ground-truth start (evaluation joins `simulator_ground_truth`).

## 2. AI investigation

```
POST /api/v1/incidents/{id}/investigate
  → AgentService.investigate: DiagnosisService.classify (if none) → diagnoses + model_predictions
  → reasoner = HeuristicReasoner (default) | LlmReasoner (LLM_PROVIDER=openai + key)
  → reasoner may ONLY call AgentTools whitelist:
      read: get_incident, get_payment_stats, get_failure_distribution, get_customer_history,
            get_revenue_at_risk (RevenueService), get_recovery_candidates
      dry-run: propose_recovery_strategy (PolicyEngine preview — decision persisted, action_id=NULL)
      mutation: request_payment_link, request_recovery_execution
                → amount copied from ORIGINAL payment/opportunity (never reasoner-supplied)
                → recovery_actions row (PROPOSED) → PolicyEngine.evaluate → outcome verbatim
  → LLM path guardrails: JSON schema validation + hallucination guard (every number must match
     a tool-result value; unverifiable claims stripped+flagged) → failure → heuristic fallback (degraded)
  → agent_reports row (report JSON, reasoner, model, confidence, escalation flag)
  → audit_logs: agent.investigate
GET  /api/v1/incidents/{id}/investigation → latest agent_reports row
```

## 3. Opportunity build + recovery plan

```
POST /api/v1/recovery/opportunities/build {incident_id}
  → OpportunityBuilder, three sources (first-write wins; a checkout is never double-counted):
     failed_payment_retry (failed payments, per-payment) · stuck_checkout_payment
     (payments still `created` ≥30 min at build time) · dropped_checkout
     (orders with no payment attempt)
  → recovery_opportunities rows (idempotent per incident+payment/order; delta-only re-runs)
  → audit_logs: recovery.opportunities_built
GET  /api/v1/recovery/{id}/plan
  → StrategyGenerator: 6 candidates (retry_payment, delayed retry, create_payment_link,
     notify_customer, escalate_human, no_action)
  → expected_recovery from RevenueService.opportunity_estimate (priors, documented)
  → confidence = diagnosis confidence × action-fit; eligibility/risk/reason/constraints
  → recommended = highest policy-eligible expected recovery (ties → lower risk)
  → NOTE: plan evaluation persists recovery_strategies + a policy preview decision (documented
     read-through side effect; idempotent find-or-create)
```

## 4. Execute — autonomous lane

```
POST /api/v1/recovery/{id}/execute {actor}
  → RecoveryExecutor: find-or-create open action → PolicyEngine.evaluate(metadata:
     {current_action_id, strategy_id, request_id})
  → ALLOWED → gateway call with gateway_request_id (gwr_) as idempotency key
     (order receipt / payment-link reference_id — Razorpay's real dedup fields)
  → EXECUTING → VERIFYING → webhook or fetch_payment reconciliation → RECOVERED | FAILED
  → every transition → audit_logs (actor + request_id)
```

## 5. Execute — approval lane

```
PolicyEngine → REQUIRES_APPROVAL (amount > ₹5000 | confidence < 0.85 | attempts exceeded)
  → action PENDING_APPROVAL; execute is refused (409) until decision
POST /approve {actor, note?} → APPROVED → execute proceeds on the stored decision
POST /reject {actor, note} (note required) → REJECTED (terminal)
POST /escalate {actor, note} (note required) → ESCALATED (terminal)
POST /cancel → CANCELLED (terminal)
```

## 6. Webhook verification

```
POST /webhooks/razorpay (raw body)
  → 1 MiB body cap → 413 (enforced before signature verification)
  → HMAC-SHA256(raw body, webhook secret) vs X-Razorpay-Signature → 400 on mismatch/missing
  → dedupe on x-razorpay-event-id (webhook_events UNIQUE) → duplicate: 200 already_processed,
     ZERO side effects
  → store raw event → handler registry: payment.captured | payment.failed | payment_link.paid
  → payment state machine is out-of-order safe: captured is terminal; failed→captured ends captured
  → linked recovery_actions: EXECUTING|VERIFYING → RECOVERED (capture/link-paid) | FAILED (failure);
     verified_at/completed_at stamped; opportunity stored status synced
  → handler failure → event processed=false (reconcilable, §8), still ack 200
```

## 7. Timeout / UNKNOWN (never a blind retry)

```
Gateway transient (timeout/connect/5xx on a MUTATION)
  → action UNKNOWN (mutation sent exactly once — wire-level asserted in tests)
  → duplicate protection: further proposals for the opportunity are blocked
  → resolution is GET-ONLY: fetch_payment/fetch_order re-query (executor.resolve)
  → truthful terminal state from gateway evidence (RECOVERED | FAILED | still UNKNOWN)
```

## 8. Reconciliation sweep (operator-triggered, ADR 0011)

```
POST /api/v1/recovery/reconcile {actor}
  → reconcile service (idempotent):
     a) UNKNOWN actions → GET-only gateway re-query → resolve where truth exists
     b) webhook_events processed=false → re-run handler through the same registry
  → report {unknown_scanned, resolved, still_unknown, webhooks_reprocessed, webhooks_still_failing}
  → audit_logs: recovery.reconcile
```
No background scheduler in v1 — the sweep is deterministic and demo-controllable.

## 9. Unsafe AI recommendation (the trust boundary working)

```
reasoner proposes refund (or any non-allowlisted action)
  → PolicyEngine: BLOCKED (allowlist / never_auto_execute.refund) — fails closed
  → action REJECTED; ZERO gateway calls (transport asserted unused in tests)
  → policy_decisions + audit_logs rows explain the block
```

## 10. Demo scenario + evaluation run

```
POST /api/v1/demo/scenario/{name} → simulator run_idempotent seed (ground truth isolated in
   simulator_ground_truth) + one anchored detection pass → summary (incidents, counts)
POST /api/v1/demo/reset → bulk-delete commerce+derived tables in FK-safe order; keep
   evaluation_runs/experiments/model_predictions/audit_logs; exactly one demo.reset audit row
POST /api/v1/evaluation/run → harness: isolated scratch DBs, two arms (baseline generic retry vs
   full loop with real services + SimulatedPaymentGateway), disclosed deterministic operator/customer
   roles → metrics persisted to evaluation_runs + experiments; GETs serve stored rows only.
   Holdout arm (default 10%): customers assigned by sha256("holdout:{seed}:{customer_id}") receive
   NO PulseRecover actions (detection still runs); incremental lift = treatment − holdout recovery
   rate with Newcombe 95% CI, class-standardized variant, strata, median TTR; ITT denominators
   snapshotted before recovery; all captures in both groups via the real signed-webhook path.
GET  /api/v1/incidents/{id} → detail also computes insights (leaf, read-time, null-safe on bad
   windows): ranked failure-facet outliers (lift vs pre-incident baseline, min-support floors,
   low-confidence flags) + platform callout (fleet-wide vs incident-specific, simulated_fleet scope).
```

## Transaction boundaries

- Policy decisions and audit rows flush into the caller's transaction (no independent commits) — a rolled-back action leaves no orphan audit.
- One commit per API operation; `opportunities/build` on large incidents commits batched rows in a single transaction (documented sqlite single-writer constraint, ADR 0002/0009).
- Exception: the reconcile sweep commits per repaired unit (a failing event rolls back only itself — earlier repairs stay durable), then commits its own audit row last (`app/services/recovery/reconcile.py`).
- The gateway layer performs no DB writes; payment/recovery state transitions happen only in the executor and the webhook handlers (`app.services.recovery.webhook_handlers` — service layer, shared by intake and the reconcile sweep) — the import directions are enforced by `tests/architecture/test_boundaries.py`.

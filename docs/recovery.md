# PulseRecover — Recovery Execution Engine

The closed loop's write side: opportunity building → strategy generation →
deterministic policy gate → bounded gateway execution → verification →
measured recovered revenue.

> Probabilistic AI proposes. Deterministic policy decides. Payment
> infrastructure executes. Verification proves.

Owner: recovery execution engineer. Code: `backend/app/services/recovery/`,
`backend/app/api/v1/recovery.py`, `backend/app/schemas/recovery.py`.
Tests: `backend/tests/recovery/`.

## 1. Pipeline overview

```
incident (detected degradation)
  │  OpportunityBuilder.build_for_incident        idempotent per incident
  ▼
recovery_opportunities      one row PER FAILED PAYMENT / per stuck-created
                            payment / per payment-less abandoned order
  │  StrategyGenerator.generate                   find-or-create, immutable
  ▼
recovery_strategies         ranked candidates + recommendation (selected=true)
  │  RecoveryExecutor.execute                     find-or-create the action
  ▼
recovery_actions            PROPOSED → POLICY_EVALUATED → … (state machine §3)
  │  PolicyEngine.evaluate                        the ONLY authorization path
  ▼
PaymentGateway (Razorpay test mode | simulator)   one mutation per action, ever
  │  webhook (payment.captured / payment_link.paid) or fetch re-query
  ▼
RECOVERED / FAILED / UNKNOWN                      verification proves the outcome
```

## 2. Opportunities: per-payment, not batched

`OpportunityBuilder` turns an incident's blast radius into opportunities from
three sources (all scoped to `window_start <= created_at < window_end`):

| source | selection | opportunity_type | natural strategy |
|---|---|---|---|
| Failed payment | `status == "failed"` | `failed_payment_retry` | `retry_payment` (transient classes) |
| Stuck checkout payment | `status == "created"` for ≥ **30 min** at build time | `stuck_checkout_payment` | `create_payment_link` |
| Abandoned order | order `created` with NO payment rows at all | `dropped_checkout` | `create_payment_link` |

- **Failed payments** in the incident window (`status == "failed"`)
  → `failed_payment_retry`.
- **Stuck checkout payments**: payments created in the window that never
  resolved and have been stuck in `created` for at least
  `STUCK_CREATED_THRESHOLD` (30 min) *at build time* →
  `stuck_checkout_payment`. This is the recovery half of detection's
  `checkout_abandonment_rate` signal (docs/detection.md): same 30-minute
  inactivity threshold, but evaluated against the builder's knowledge edge
  (now), not the detection pass's window end — a payment created late in the
  window qualifies once it is genuinely stuck, and a payment younger than
  30 minutes is honestly left alone (it may still be in flight). The payment
  carries no error fields, so the strategy generator classifies it
  `abandonment` directly — the stuck state IS the abandonment signal.
- **Abandoned checkouts**: orders still `created` with NO payment rows at all
  → `dropped_checkout` (`meta.order_id` tracks the source order).

**Dedup rule — exactly one opportunity per (incident, checkout):**

- *Payment-level wins at selection time.* An order with ANY payment row
  (failed or stuck-created) is excluded from the `dropped_checkout` path —
  the payment's own opportunity already covers that checkout.
- *First-write wins across builds.* A stuck payment whose order is already
  represented in this incident — by an order-level `dropped_checkout` from an
  earlier build, or by a sibling stuck attempt on the same order — is skipped
  and reported in `BuildResult.existing`; the existing opportunity already
  carries the checkout's customer and amount and its payment-link recovery is
  identical.
- Two DISTINCT payments on one order (a failed attempt plus a later stuck
  one) are NOT merged: they are separate attempts, and the policy gate's
  duplicate protection prevents double-firing the customer.

Per-payment granularity is a hard requirement, not a style choice:

1. **Verification** links actions to gateway truth via
   `recovery_opportunities.payment_id` (the webhook reconciler resolves
   actions through exactly this column). A batch could never prove WHICH
   payment was recovered.
2. **Policy guards** (per-customer rate limits, duplicate protection) key off
   the opportunity's customer — per-payment rows keep them precise.
3. **Idempotency** is per (incident, payment) / (incident, order): re-running
   the builder after new webhook arrivals adds only the delta. Proven by
   `tests/recovery/test_builder.py::TestIdempotency` and
   `tests/recovery/test_stuck_checkout.py::TestIdempotency`.

Known estimate caveat: `RevenueService.opportunity_estimate` classifies a
stuck-created payment from its (empty) error fields as `unknown`
(recoverability 0.10), so expected-recovery paise for
`stuck_checkout_payment` opportunities are deliberately conservative; the
strategy RANKING is unaffected (link still first). Teaching the revenue
engine to fall back to the opportunity-type class when payment
classification is `unknown` is a flagged revenue-track follow-up.

## 3. Strategy generation and the plan table

`StrategyGenerator.generate(opportunity)` writes six candidates
(find-or-create; regeneration is a no-op so policy decisions and actions
always reference the proposal of record):

| candidate | action_type | constraints | typical fit |
|---|---|---|---|
| immediate retry | `retry_payment` | `{}` | timeouts / soft declines |
| delayed retry | `retry_payment` | `{"delay_seconds": 1800}` | insufficient funds (payday effect) |
| payment link | `create_payment_link` | `{}` | abandonment, customer must re-attempt |
| notify | `notify_customer` | `{"channel": "notification"}` | nudge; no money moves |
| escalate | `escalate_human` | `{"queue": "human_ops"}` | always eligible fallback |
| baseline | `no_action` | `{}` | comparison anchor, expected = 0 |

Delayed retry is not a separate `ActionType` — per `app/ports.py` it is
`retry_payment` + `constraints.delay_seconds`. The monolith has no scheduler,
so the executor fires immediately and records the requested delay in the
gateway order's notes; the constraint is part of the audited proposal.

Each candidate carries:

- `expected_recovery_paise` — from `RevenueService.opportunity_estimate`
  (recoverability prior × strategy effectiveness prior; integer paise).
- `confidence` — `evidence_strength × action_fit`, where evidence_strength is
  the latest ML diagnosis confidence for the incident, or **0.80** when no
  diagnosis exists. Since the policy auto-execute floor is **0.85**, only
  diagnosis-backed proposals can auto-execute; everything else takes the
  human-approval lane. `action_fit` is a documented per-(action,
  failure-class) prior table in `strategies.py`.
- `risk` (`low|medium|high`), `eligibility`, `reason`, `constraints`.

Eligibility is a hard pre-filter (not policy — policy re-checks everything):
retry requires a linked **failed** payment and is disabled for hard declines
(network rules discourage resubmission) — a payment stuck in `created` has no
failed charge to resubmit, so retry is ineligible there and the payment link
is the recovery vehicle; payment links require `amount >= 100` paise; notify
requires a non-opted-out customer.

**Recommendation rule:** the eligible candidate with the highest
`expected_recovery_paise`; ties break to lower risk, then candidate order.
`GET /api/v1/recovery/{id}/plan` returns the full comparison table with
`recommended_strategy_id` plus a `policy_preview` — a real, persisted
evaluation of the recommended strategy through the gate (actor
`system:plan_preview`), so the UI shows what the deterministic gate would
decide *right now* (stopping rules and rate limits included).

## 4. The recovery_actions state machine

```
PROPOSED ──evaluate──▶ POLICY_EVALUATED ──┬─ ALLOWED ───────────▶ EXECUTING
                                          ├─ REQUIRES_APPROVAL ▶ PENDING_APPROVAL
                                          └─ BLOCKED ──────────▶ REJECTED (terminal)
PENDING_APPROVAL ──approve──▶ APPROVED ──▶ EXECUTING
PENDING_APPROVAL ──reject───▶ REJECTED (terminal)
PROPOSED/POLICY_EVALUATED/PENDING_APPROVAL/APPROVED ──cancel──▶ CANCELLED (terminal)
any non-terminal state ──escalate──▶ ESCALATED (terminal; human owns it)

EXECUTING ──gateway 4xx──▶ FAILED (terminal, definitive: nothing happened)
EXECUTING ──response──▶ VERIFYING ──webhook / inline / fetch──▶ RECOVERED
EXECUTING ──timeout/5xx/unreadable──▶ UNKNOWN (ambiguous — never guessed)
UNKNOWN ──resolve() re-query──▶ RECOVERED   (only on positive gateway evidence)
```

Notes:

- There is no `BLOCKED` status: a policy-blocked action ends `REJECTED` with
  the decision linked (`policy_decision_id`) and `note = "blocked by the
  deterministic policy gate"`. REJECTED/CANCELLED consume no attempt budget
  and do not trigger duplicate protection — nothing reached the gateway.
- `payment.failed` is not terminal for payments; the webhook reconciler can
  still move a `FAILED` action to `RECOVERED` on a late capture
  (see `app/api/v1/webhooks.py`).
- The opportunity's *displayed* status is projected from its latest action at
  read time (actions are the source of truth; webhook reconciliation updates
  them directly). The stored `status` column is shadowed by the executor on
  its own transitions.
- `APPROVED` executes on the decision already made; re-evaluation would loop
  (the confidence that required approval has not changed). Hard-blocked
  actions can never reach `APPROVED` — `BLOCKED` has no approval path.

Every transition — including the agent-visible `PROPOSED` creation, the
policy evaluation, and inconclusive resolve re-queries
(`recovery.action.resolve_check`) — appends an `audit_logs` row with actor
and request_id. Proven by
`tests/recovery/test_executor.py::TestAuditTrail`.

## 5. Idempotency design

Four independent layers; each proven by a dedicated test.

1. **One open action per opportunity.** `execute()` is find-or-create: a
   second execute reuses the action in
   `PROPOSED/POLICY_EVALUATED/PENDING_APPROVAL/APPROVED/EXECUTING/VERIFYING/UNKNOWN`
   instead of creating a new one. In-flight (`EXECUTING/VERIFYING`) → 409;
   `PENDING_APPROVAL` → 409 until a human approves.
2. **`gateway_request_id` as the gateway idempotency key.** Minted once per
   action (`gwr_<uuid32>`, 36 chars — inside Razorpay's 40-char limit),
   `UNIQUE` column, mapped to order `receipt` / payment-link `reference_id`
   (the only dedupe primitives Razorpay offers for these APIs — see
   docs/research.md). A replayed mutation with the same key is a gateway-side
   400 duplicate, which maps to a definitive `FAILED`, never a double charge.
3. **Policy duplicate protection.** Cross-opportunity duplicates (same
   customer + action type inside the 60-minute cooldown) are BLOCKED while
   the prior action is active. `RECOVERED` and `UNKNOWN` count as active:
   never double-collect, never re-fire an action whose outcome is unclear.
4. **Webhook dedup** on `x-razorpay-event-id` (UNIQUE) — owned by the
   webhook module; the executor relies on it for exactly-once verification
   side effects.

## 6. Failure-mode table

| # | Scenario | System behavior | Proof (tests/recovery/test_failure_modes.py) |
|---|---|---|---|
| 1 | Gateway timeout / 5xx / unreadable response on the mutating call | `GatewayTransientError` → action `UNKNOWN`, attempt consumed, **no blind retry**; re-execute performs a GET-only re-query (`fetch_payment`/`fetch_order`); duplicate proposals BLOCKED by `duplicate.cooldown`; when the gateway later shows the payment captured, resolve moves it to `RECOVERED` | `TestTimeoutUnknownResolution::test_full_scenario` (httpx.MockTransport; asserts exactly one POST ever) |
| 2 | AI proposes a refund | `refund` is not on the policy allowlist AND is in `never_auto_execute` → `BLOCKED` → action `REJECTED`; **zero gateway calls**; block mirrored to `audit_logs` | `TestRefundHasNoExecutionPath` (asserts the mock transport saw no request) |
| 3 | Duplicate execute request | First execute fires once; the duplicate is BLOCKED by policy duplicate protection (RECOVERED stays active for cooldown) | `TestDuplicateExecute` (asserts `len(sim.payment_links) == 1`) |
| 4 | Confidence below 0.85 | Gate returns `REQUIRES_APPROVAL` → `PENDING_APPROVAL`; further `execute` is refused (409) until `approve`; then exactly one gateway call | `TestApprovalGate` |
| 5 | Three consecutive FAILED actions on one incident | The stopping rule (`stopping_rule.incident`) BLOCKS the fourth action before any gateway call; a human must review | `TestStoppingRule` |
| 6 | Gateway 4xx on execution | Definitive rejection — nothing happened → `FAILED` (terminal), `last_error` carries the gateway reason | `TestStoppingRule` (each of the 3 failures) |
| 7 | Resolve re-query inconclusive (payment still failed / not found / gateway still down) | Action stays `UNKNOWN`; evidence recorded in `recovery.action.resolve_check` audit rows. UNKNOWN is surfaced, never silently counted as recovered | `TestTimeoutUnknownResolution` |

## 7. API surface (`/api/v1/recovery`, opportunity-centric)

| method & path | purpose |
|---|---|
| `GET /opportunities` | list; filters `status`, `incident_id`, `opportunity_type`, `customer_id`; pagination |
| `POST /opportunities/build` | `{incident_id}` → idempotent opportunity build + strategy generation |
| `GET /{opportunity_id}` | detail: actions with linked policy decisions + full audit refs |
| `GET /{opportunity_id}/plan` | strategy comparison table + recommendation + policy preview |
| `POST /{opportunity_id}/execute` | find-or-create action → policy gate → fire if ALLOWED; resolves UNKNOWN by re-query |
| `POST /{opportunity_id}/approve` | `PENDING_APPROVAL → APPROVED` (actor from body) |
| `POST /{opportunity_id}/reject` | → `REJECTED` (action-level, or opportunity-level when no action exists) |
| `POST /{opportunity_id}/escalate` | → `ESCALATED` from any non-terminal state (human handoff) |
| `POST /{opportunity_id}/cancel` | pre-execution states only → `CANCELLED` |

Mutating routes require `X-API-Key`; the actor (`human:console`,
`agent:strategist`, …) travels in the request body and lands on every audit
row and policy decision. `X-Request-ID` propagates end to end.

Domain errors: 404 unknown opportunity/strategy; 409 invalid state
(execute while in-flight, execute while awaiting approval, cancel after
firing, approve with nothing pending, switching strategies on an open action).

## 8. Gateway action mapping

| action_type | gateway call | verification |
|---|---|---|
| `retry_payment` | `create_order` (fresh payable order; `receipt` = `gateway_request_id`) | order `amount_paid` inline, else webhook `payment.captured` on the linked payment, else `resolve()` fetch |
| `create_payment_link` | `create_payment_link` (`reference_id` = `gateway_request_id`) | link `status == "paid"` inline, else webhook `payment_link.paid` |
| `notify_customer` | no gateway mutation (recorded; no notification worker in the monolith) | `VERIFYING` until the customer's payment webhook lands |
| `escalate_human` / `no_action` | none | terminal immediately (`ESCALATED` / `CANCELLED`) |
| subscription actions | no executor mapping → definitive `FAILED` | — |

Razorpay has no "retry a payment" API; the fresh order IS the retry
primitive, and its unique `receipt` is what makes retrying safe.

# Payment-Action Invariants — the 12-point mechanical proof checklist

Every invariant that protects money in PulseRecover, each mapped to the exact
test(s) that prove it. This is a **checklist, mechanically proven**: every
`file::test` reference below is a real pytest node id (verified by script —
each one collects and passes), not prose.

- Proof suites: `backend/tests/recovery/`, `backend/tests/razorpay/`,
  `backend/tests/security/`, `backend/tests/policy/`, `backend/tests/agent/`
  (pre-existing) and **`backend/tests/invariants/`** (this package — proofs
  that did not exist anywhere else).
- Run the new proofs:
  `cd backend && .venv/Scripts/python -m pytest tests/invariants -q`
  (30 tests, green as of 2026-08-28).
- Run everything: `cd backend && .venv/Scripts/python -m pytest -q`.
- Demo commands below assume `cd backend` on Windows Git Bash
  (`.venv/Scripts/python`).

## Critical finding found and fixed while building this checklist

**Concurrent duplicate execute double-fired the gateway on Postgres.**
Invariant 1's concurrent clause was NOT true on the compose deploy stack
(Postgres 16): two barrier-synchronized `POST /api/v1/recovery/{id}/execute`
requests, with the gateway slowed 0.4–0.5 s to widen the race window,
produced **TWO payment links, both RECOVERED — 3/3 runs** (verified live
2026-08-28, throwaway `postgres:16-alpine` container, file-backed race
harness). Root cause: the executor's open-action check and the policy
duplicate guard both read committed state; under READ COMMITTED two
transactions can both see "no open action" and both fire. SQLite was safe by
accident (writer serialization orders the race), which is why the existing
suite never caught it.

**Fix (small, documented):** `RecoveryExecutor.execute` now takes
`SELECT ... FOR UPDATE` on the opportunity row first
(`backend/app/services/recovery/executor.py::_lock_opportunity`). Postgres
serializes concurrent executors on that row; the loser waits for the
winner's commit, then sees its action and is cleanly REJECTED by the
`duplicate.cooldown` policy guard (or 409 if still in flight). The clause is
silently omitted on SQLite, so local/test behavior is unchanged.
Post-fix verification, same live Postgres harness: **3/3 runs → exactly ONE
link; loser 200 REJECTED via duplicate.cooldown**. Regression test:
`tests/invariants/test_concurrent_execute.py` (runs the race on the test
stack; the Postgres before/after runs above are the live evidence anchor).

---

## I1 — One logical action → no duplicate gateway mutations (incl. concurrent duplicate execute)

- **Statement:** An approved recovery action causes at most ONE mutating
  gateway call, ever — on retry, timeout, duplicate execute, or a concurrent
  double-click. The idempotency key (`recovery_actions.gateway_request_id`,
  UNIQUE) maps to Razorpay `receipt`/`reference_id`; mutating calls are never
  retried after a transient error; a second execute is refused in-flight and
  policy-BLOCKED after recovery.
- **Threat it kills:** double-charging a customer / double-firing a recovery
  (the cardinal payment sin), including the two-tab race.
- **Proof tests:**
  - `backend/tests/invariants/test_concurrent_execute.py::test_concurrent_duplicate_execute_exactly_one_gateway_mutation` — the concurrent race itself (barrier-synchronized, slowed gateway, independent DB sessions per thread; 3 rounds)
  - `backend/tests/recovery/test_executor.py::TestGuards::test_execute_refuses_in_flight_action` — EXECUTING/VERIFYING → 409, no second fire
  - `backend/tests/recovery/test_failure_modes.py::TestDuplicateExecute::test_second_execute_is_policy_blocked_after_recovery` — duplicate execute after RECOVERED → policy BLOCKED, one gateway call total
  - `backend/tests/recovery/test_failure_modes.py::TestTimeoutUnknownResolution::test_full_scenario` — timeout → UNKNOWN → re-execute makes NO new mutation, ever
  - `backend/tests/security/test_gateway_inconsistency.py::TestTimeoutsBounded::test_mutating_call_never_retried_on_timeout` — transport never blind-retries a mutation
  - `backend/tests/razorpay/test_client.py::test_timeout_on_mutation_raises_transient_with_single_send` — same at the HTTP client layer
  - `backend/tests/razorpay/test_simulated.py::test_order_receipt_dedupe_mirrors_razorpay` and `backend/tests/razorpay/test_simulated.py::test_payment_link_reference_id_dedupe_mirrors_razorpay` — gateway-side idempotency mirror
  - `backend/tests/policy/test_policy_engine.py::TestDuplicateProtection::test_duplicate_blocked_within_cooldown` and `backend/tests/policy/test_policy_engine.py::TestDuplicateProtection::test_recovered_action_still_blocks_duplicates` — the cross-action duplicate guard
- **Demo:** `.venv/Scripts/python -m pytest tests/invariants/test_concurrent_execute.py -q`

## I2 — UNKNOWN never becomes RECOVERED without positive gateway evidence

- **Statement:** An action whose gateway outcome is ambiguous (timeout /
  5xx / unreadable response) stays UNKNOWN until a re-query
  (`fetch_payment`/`fetch_order`) or a properly-verified webhook returns
  positive, amount-checked, identity-checked evidence. No timeout sweep, no
  crash, no heuristics ever mark it recovered.
- **Threat it kills:** fake "recovered revenue" — claiming money that was
  never collected, corrupting every metric the product reports.
- **Proof tests:**
  - `backend/tests/recovery/test_reconcile.py::test_unknown_action_stays_unknown_without_gateway_truth` — sweep leaves it UNKNOWN (+ audit)
  - `backend/tests/recovery/test_reconcile.py::test_unknown_action_resolves_once_gateway_truth_appears` — and resolves it when truth appears
  - `backend/tests/recovery/test_failure_modes.py::TestTimeoutUnknownResolution::test_full_scenario` — full timeout→UNKNOWN→resolve arc
  - `backend/tests/security/test_gateway_inconsistency.py::TestTimeoutsBounded::test_reconcile_sweep_completes_with_hanging_gateway` — hanging gateway: sweep terminates, 0 resolved, 0 mutations
  - `backend/tests/security/test_payment_link_verification.py::TestPaymentLinkAmountVerification::test_partial_paid_status_not_recovered`, `backend/tests/security/test_payment_link_verification.py::TestPaymentLinkAmountVerification::test_paid_status_but_underpaid_amount_paid_not_recovered`, `backend/tests/security/test_payment_link_verification.py::TestPaymentLinkAmountVerification::test_missing_amount_fails_closed` — "paid" claims that don't exactly match the amount/currency do NOT recover
- **Demo:** `.venv/Scripts/python -m pytest tests/recovery/test_reconcile.py::test_unknown_action_stays_unknown_without_gateway_truth -q`

## I3 — Gateway entity identity must match the intended entity

- **Statement:** When resolving via gateway fetches, the returned entity id
  must equal the requested id. A `fetch_payment`/`fetch_order` answering for
  a DIFFERENT id proves nothing — the action stays UNKNOWN and the mismatch
  is audited.
- **Threat it kills:** identity confusion — a confused/buggy/malicious
  gateway flipping UNKNOWN actions to RECOVERED with somebody else's
  captured payment.
- **Proof tests** (regression tests written when this exact bug was fixed):
  - `backend/tests/security/test_gateway_inconsistency.py::TestResolveIdentityConfusion::test_fetch_payment_wrong_id_does_not_resolve_recovered`
  - `backend/tests/security/test_gateway_inconsistency.py::TestResolveIdentityConfusion::test_fetch_order_wrong_id_does_not_resolve_recovered`
  - `backend/tests/security/test_gateway_inconsistency.py::TestResolveIdentityConfusion::test_fetch_payment_matching_id_still_recovers` — the honest path still works
- **Demo:** `.venv/Scripts/python -m pytest tests/security/test_gateway_inconsistency.py::TestResolveIdentityConfusion -q`

## I4 — Webhook signatures verified before any processing (fail-closed, no secret)

- **Statement:** `X-Razorpay-Signature` (HMAC-SHA256 over the RAW body) is
  verified before JSON parsing, before storage, before dispatch. Bad /
  missing / tampered → 400 with zero rows written. With no webhook secret
  configured, verification fails closed — every delivery is rejected.
- **Threat it kills:** attacker-forged `payment.captured` events minting
  fake recoveries (the webhook endpoint is unauthenticated by design — the
  signature IS the auth).
- **Proof tests:**
  - `backend/tests/razorpay/test_webhooks.py::test_invalid_signature_rejected_400_nothing_stored` — bad signature → 400 AND `webhook_events` stays empty
  - `backend/tests/razorpay/test_webhooks.py::test_tampered_body_rejected` and `backend/tests/razorpay/test_webhooks.py::test_missing_signature_rejected`
  - `backend/tests/razorpay/test_client.py::test_verify_webhook_signature_fails_closed_without_secret` — port-level fail-closed
  - `backend/tests/invariants/test_webhook_no_secret_fail_closed.py::test_route_rejects_every_delivery_when_no_secret_configured` — route-level fail-closed: empty configured secret → signed request still 400, payment untouched
  - `backend/tests/security/test_webhook_adversarial.py::TestMalformedWebhookBodies::test_oversized_body_rejected_before_processing` — the 1 MiB cap fires before HMAC/parsing (413)
- **Demo:** `.venv/Scripts/python -m pytest tests/invariants/test_webhook_no_secret_fail_closed.py -q`

## I5 — Duplicate webhook events → zero duplicate side effects (incl. concurrent delivery)

- **Statement:** Razorpay delivers at-least-once. A repeated
  `x-razorpay-event-id` — sequential OR racing concurrently — is acked 200
  `already_processed` with exactly one stored event row and exactly one set
  of side effects (the `webhook_events.gateway_event_id` UNIQUE constraint
  decides the race).
- **Threat it kills:** retry-storm replays double-counting captures /
  double-recovering actions.
- **Proof tests:**
  - `backend/tests/razorpay/test_webhooks.py::test_duplicate_event_id_already_processed_zero_side_effects` — sequential duplicate: one event row, one payment transition
  - `backend/tests/security/test_webhook_adversarial.py::TestConcurrentDuplicateDeliveries::test_racing_duplicate_deliveries_exactly_one_side_effect` — two racing deliveries: one `received`, one `already_processed`, one capture side effect
- **Demo:** `.venv/Scripts/python -m pytest tests/security/test_webhook_adversarial.py::TestConcurrentDuplicateDeliveries -q`

## I6 — Policy evaluation always precedes any financial mutation

- **Statement:** No code path reaches the payment gateway without a
  persisted policy decision authorizing that exact action (ALLOWED), or a
  persisted REQUIRES_APPROVAL plus a recorded human approval. This includes
  the agent `request_*` tools path: the tools evaluate and persist a
  decision, never fire, and the executor re-gates their rows before firing.
- **Threat it kills:** an AI (or a bug, or an injection) bypassing the
  deterministic gate and moving money on its own authority.
- **Proof tests** (the spy gateway fails the test if ANY mutation arrives
  without a persisted authorizing decision, checked at the transport seam):
  - `backend/tests/invariants/test_policy_precedes_gateway.py::test_auto_execute_persists_allow_decision_before_fire`
  - `backend/tests/invariants/test_policy_precedes_gateway.py::test_human_approval_lane_authorizes_before_fire`
  - `backend/tests/invariants/test_policy_precedes_gateway.py::test_agent_tool_row_is_regated_before_fire_and_tool_never_fires`
  - `backend/tests/invariants/test_policy_precedes_gateway.py::test_blocked_action_persists_decision_and_never_fires`
  - `backend/tests/invariants/test_policy_precedes_gateway.py::test_agent_tools_have_no_gateway_handle` — structural: AgentTools is constructed without a gateway
  - `backend/tests/policy/test_policy_engine.py::TestPersistenceAndAudit::test_every_decision_is_persisted` — every evaluate() persists
  - `backend/tests/agent/test_tools.py::test_request_payment_link_creates_proposed_row_with_original_amount` and `backend/tests/agent/test_tools.py::test_high_confidence_small_retry_auto_allows` — tool behavior at both outcomes
- **Demo:** `.venv/Scripts/python -m pytest tests/invariants/test_policy_precedes_gateway.py -q`

## I7 — A refund can never autonomously execute

- **Statement:** REFUND is (a) absent from the shipped policy allowlist,
  (b) present in `never_auto_execute` (hard block, no approval lane),
  (c) unmapped in the executor's dispatcher — and (d) **has no transport at
  all**: neither the `PaymentGateway` port nor either implementation
  (Razorpay REST client, simulator) exposes a refund method.
- **Threat it kills:** an AI-proposed (or injection-proposed) refund
  draining the merchant — the highest-blast-radius action type simply does
  not exist downstream of the gate.
- **Proof tests:**
  - `backend/tests/invariants/test_refund_no_transport.py::test_gateway_port_defines_no_refund_transport` and `backend/tests/invariants/test_refund_no_transport.py::test_gateway_implementations_have_no_refund_transport` — the zero-transport proof
  - `backend/tests/invariants/test_refund_no_transport.py::test_executor_dispatch_has_no_refund_mapping` — even a forged REFUND row dies definitively, zero gateway calls
  - `backend/tests/recovery/test_failure_modes.py::TestRefundHasNoExecutionPath::test_refund_blocked_and_gateway_never_touched` — AI-proposed refund: BLOCKED, transport never touched
  - `backend/tests/policy/test_policy_engine.py::TestHardBlocks::test_ai_proposed_refund_blocked`, `backend/tests/policy/test_policy_engine.py::TestHardBlocks::test_refund_is_not_even_on_the_allowlist`, `backend/tests/policy/test_policy_engine.py::TestHardBlocks::test_refund_as_raw_string_is_coerced_then_blocked`
  - `backend/tests/policy/test_config_loader.py::TestRealDefaultFile::test_allowlist_is_closed_and_excludes_refund` and `backend/tests/policy/test_config_loader.py::TestRealDefaultFile::test_hard_blocks_and_stopping_rule` — the SHIPPED config's allowlist/never_auto
  - `backend/tests/agent/test_tools.py::test_propose_recovery_strategy_refund_is_blocked_and_creates_nothing` and `backend/tests/agent/test_tools.py::test_request_recovery_execution_refund_is_blocked_with_no_execution_path`
  - `backend/tests/security/test_prompt_injection.py::TestLlmPathTreatsInjectionAsInertData::test_model_proposing_refund_and_approval_bypass_is_contained` — injection-proposed refund contained
- **Demo:** `.venv/Scripts/python -m pytest tests/invariants/test_refund_no_transport.py -q`

## I8 — Amount limits enforced on every entry path

- **Statement:** Above ₹5,000 (500,000 paise) no action auto-executes — it
  takes the human-approval lane — on EVERY entry path: agent tool, strategy
  execute, direct API. The amount always comes from the original
  payment/opportunity row, never from caller input.
- **Threat it kills:** big-ticket autonomous mistakes; an LLM (or caller)
  inflating an amount.
- **Proof tests:**
  - `backend/tests/security/test_safety_invariants.py::TestExcessiveAmounts::test_agent_tool_path_routes_to_approval`
  - `backend/tests/security/test_safety_invariants.py::TestExcessiveAmounts::test_api_execute_path_routes_to_approval_then_human_lane_works`
  - `backend/tests/security/test_safety_invariants.py::TestExcessiveAmounts::test_direct_strategy_execute_cannot_bypass_ceiling`
  - `backend/tests/policy/test_policy_engine.py::TestApprovalThresholds::test_amount_above_5000_inr_requires_approval` and `backend/tests/policy/test_policy_engine.py::TestApprovalThresholds::test_amount_exactly_5000_inr_is_within_bounds` — the boundary itself
  - `backend/tests/security/test_input_abuse.py::TestExtremeNumericInputs::test_int64_max_amount_takes_approval_lane_via_agent_tool` — even int64-max
- **Demo:** `.venv/Scripts/python -m pytest tests/security/test_safety_invariants.py::TestExcessiveAmounts -q`

## I9 — Stopping rules enforced (3 consecutive failures → block; success resets)

- **Statement:** After 3 consecutive FAILED recovery actions on an incident
  (and per strategy), further automation is policy-BLOCKED until a human
  reviews. A success anywhere in the run resets the streak; three fresh
  failures re-arm it.
- **Threat it kills:** the automation hammering a broken gateway/customer
  forever — fail-loop burn.
- **Proof tests:**
  - `backend/tests/recovery/test_failure_modes.py::TestStoppingRule::test_fourth_action_is_blocked_after_three_failures` — the 4th action never reaches the gateway
  - `backend/tests/security/test_safety_invariants.py::TestStoppingRuleSemantics::test_streak_counts_across_strategy_boundaries`
  - `backend/tests/security/test_safety_invariants.py::TestStoppingRuleSemantics::test_success_resets_the_streak`
  - `backend/tests/security/test_safety_invariants.py::TestStoppingRuleSemantics::test_three_fresh_failures_after_success_rearm_the_rule`
  - `backend/tests/policy/test_policy_engine.py::TestStoppingRules::test_stopping_rule_fires_from_recorded_history`, `backend/tests/policy/test_policy_engine.py::TestStoppingRules::test_two_consecutive_failures_do_not_trip`, `backend/tests/policy/test_policy_engine.py::TestStoppingRules::test_a_recovery_breaks_the_strategy_streak` — engine-level semantics
- **Demo:** `.venv/Scripts/python -m pytest tests/security/test_safety_invariants.py::TestStoppingRuleSemantics -q`

## I10 — Customer opt-out enforced on every action type

- **Statement:** An opted-out customer is a HARD BLOCK (no approval lane)
  for every customer-contacting action type, on the API path AND the agent
  tool path — zero gateway calls. Opt-out also disables the notify strategy
  at generation time.
- **Threat it kills:** contacting / charging a customer who revoked consent
  (regulatory + trust).
- **Proof tests:**
  - `backend/tests/security/test_safety_invariants.py::TestCustomerOptOut::test_opted_out_customer_blocked_for_every_action_type` — retry, link, notify: all BLOCKED, mirrored to audit
  - `backend/tests/security/test_safety_invariants.py::TestCustomerOptOut::test_opted_out_customer_blocked_via_agent_tool_path`
  - `backend/tests/policy/test_policy_engine.py::TestOptedOutCustomer::test_opted_out_customer_blocked` and `backend/tests/policy/test_policy_engine.py::TestOptedOutCustomer::test_opted_out_blocks_even_high_confidence_small_amount`
  - `backend/tests/recovery/test_strategies.py::TestEligibility::test_opted_out_customer_disables_notify` — strategy-level suppression
- **Demo:** `.venv/Scripts/python -m pytest tests/security/test_safety_invariants.py::TestCustomerOptOut -q`

## I11 — Malformed confidence (NaN / Inf / negative / >1 / non-numeric) cannot reach execution

- **Statement:** Every surface fails closed on malformed confidence: the
  agent mutation tools raise BEFORE any row exists (NaN would otherwise
  crash the INSERT as a NULL bind), the dry-run surface passes the value to
  the gate which BLOCKs it as `malformed.confidence`, and a forged row with
  an out-of-range confidence is BLOCKED at the executor's re-gate with zero
  gateway calls.
- **Threat it kills:** LLM tool-call JSON (Python's `json` accepts
  `NaN`/`Infinity`) smuggling garbage past the gate into an execution slot.
- **Proof tests:**
  - `backend/tests/policy/test_policy_engine.py::TestFailClosed::test_invalid_confidence_blocked` — gate blocks nan / inf / -0.1 / 1.01 / "high"
  - `backend/tests/security/test_input_abuse.py::TestExtremeNumericInputs::test_nan_and_inf_confidence_fail_closed_via_agent_tool` — NaN/±Inf → ToolError, zero rows
  - `backend/tests/security/test_input_abuse.py::TestExtremeNumericInputs::test_non_numeric_confidence_raises_before_any_row` — non-numeric → TypeError before any row
  - `backend/tests/invariants/test_confidence_fail_closed.py::test_out_of_range_confidence_rejected_by_agent_mutation_tools` — finite out-of-range (-0.5, 1.5) → ToolError, zero rows
  - `backend/tests/invariants/test_confidence_fail_closed.py::test_executor_gate_blocks_out_of_range_confidence_with_zero_gateway_calls` — forged row re-gated: BLOCKED, transport untouched
- **Demo:** `.venv/Scripts/python -m pytest tests/invariants/test_confidence_fail_closed.py -q`

## I12 — Every financial state transition writes an audit row

- **Statement:** For every `recovery_actions` (and opportunity-level)
  transition there is an `audit_logs` row carrying actor, request id, and
  `from_status`/`to_status`; the rows form a contiguous chain from creation
  to the action's actual final status. Property-style sweep across the whole
  transition table: auto-execute, approval lane, reject, cancel, escalate,
  4xx→FAILED, timeout→UNKNOWN, resolve (both outcomes), webhook-driven
  verification, agent-tool-created rows, in-flight refusal (no new rows),
  opportunity-level transitions.
- **Threat it kills:** an untraceable mutation — the "who approved/fired
  this?" question having no answer after an incident.
- **Proof tests:**
  - `backend/tests/invariants/test_audit_transition_sweep.py` — 13 tests, one property over every path:
    `TestExecutorDrivenTransitions` (10), `TestAgentToolCreatedRows`, `TestWebhookDrivenTransitions`, `TestOpportunityLevelTransitions`
  - `backend/tests/recovery/test_executor.py::TestAuditTrail::test_every_transition_is_audited_with_actor_and_request_id` — the canonical happy-path chain
  - `backend/tests/policy/test_policy_engine.py::TestPersistenceAndAudit::test_blocked_decisions_are_mirrored_to_audit_logs` — blocked decisions are audited too
  - `backend/tests/policy/test_audit.py::TestRecord::test_writes_a_complete_row` — the audit writer contract
- **Note (observed, not a violation):** the agent `request_*` tools record
  row creation + policy evaluation in a SINGLE `agent.action_requested`
  audit row whose details carry `policy_outcome` but not structured
  `from_status`/`to_status` fields; the sweep therefore starts the from/to
  chain at the executor's first transition for tool-created rows. Every
  executor-driven transition (the ones that can move money) carries full
  from/to. Follow-up candidate, not required for the invariant.
- **Demo:** `.venv/Scripts/python -m pytest tests/invariants/test_audit_transition_sweep.py -q`

---

## Summary matrix

| # | Invariant | Proven by (primary) | New in this package? |
|---|---|---|---|
| I1 | No duplicate gateway mutations (incl. concurrent) | `tests/invariants/test_concurrent_execute.py` + executor/failure-mode/client tests | **YES** (+ Postgres race fix) |
| I2 | UNKNOWN ≠ RECOVERED without gateway truth | `tests/recovery/test_reconcile.py`, `test_failure_modes.py`, link-verification tests | no |
| I3 | Entity identity match on resolve | `tests/security/test_gateway_inconsistency.py::TestResolveIdentityConfusion` | no |
| I4 | Signature before processing, fail-closed | `tests/razorpay/test_webhooks.py` + `tests/invariants/test_webhook_no_secret_fail_closed.py` | YES (route-level no-secret) |
| I5 | Duplicate webhooks → zero side effects | `tests/razorpay/test_webhooks.py` + `test_webhook_adversarial.py` race | no |
| I6 | Policy before any mutation (all paths) | `tests/invariants/test_policy_precedes_gateway.py` (spy-gateway sweep) | **YES** |
| I7 | Refund never executes | `tests/invariants/test_refund_no_transport.py` + policy/agent/failure-mode tests | YES (zero-transport) |
| I8 | Amount limits on every entry path | `tests/security/test_safety_invariants.py::TestExcessiveAmounts` | no |
| I9 | Stopping rules + reset | `tests/recovery/test_failure_modes.py` + `TestStoppingRuleSemantics` | no |
| I10 | Opt-out on every action type | `tests/security/test_safety_invariants.py::TestCustomerOptOut` | no |
| I11 | Malformed confidence fail-closed | policy/input-abuse + `tests/invariants/test_confidence_fail_closed.py` | YES (finite out-of-range + executor re-gate) |
| I12 | Every transition audited | `tests/invariants/test_audit_transition_sweep.py` (13-test property sweep) | **YES** |

**Verification anchor:** all node ids above were script-checked to collect
and pass on 2026-08-28 (`pytest --collect-only` per reference; full suite
green). Environment: Windows 11, Python 3.12 venv, SQLite (test) +
postgres:16-alpine (live race verification).

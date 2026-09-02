# Flows G/H/I — Recovery Strategy, Execution, Evaluation (Audit Phase 4)

Auditor: agent GHI | Completed: 2026-09-02 | Repo: D:/Razorpay @ dcef95a
Status vocabulary: WORKING / PARTIALLY_WORKING / BROKEN / MOCKED / SIMULATED / UNIMPLEMENTED / UNCERTAIN.
Every claim carries evidence (path:line, command output, or live capture). Unverifiable → UNCERTAIN.
Live probes (light GETs only, 2026-09-02T08:45–08:47Z): `/api/v1/system/health`, recovery opportunities list + approvals-summary, evaluation runs/metrics/run-detail — all against `https://pulserecover-api.onrender.com`.

## FLOW G — Opportunity → Strategy → Pricing → Ranking → Policy → Approval lanes

- Status: **WORKING** as designed — with the design consequence that the auto-execute lane effectively never fires (live run: 0 ALLOWED of 1 289 gate decisions; see I.5 and findings #1).
- Pipeline: `RecoveryOpportunity` row → `StrategyGenerator.generate()` → 6 persisted `recovery_strategies` candidates → per-candidate `expected_recovery_paise` from `RevenueService.opportunity_estimate` → deterministic ranking → `PolicyEngine` decision (allow / requires-approval / block) → approval lanes.

### G.1 Opportunity intake

- `StrategyGenerator.generate(opportunity, generated_by="heuristic")` accepts a `RecoveryOpportunity` ORM row or id; raises `ValueError` if not found (`backend/app/services/recovery/strategies.py:116-120`).
- **Idempotency / immutability**: if strategies already exist for the opportunity, `generate` returns the persisted rows unchanged — "the persisted candidates are the proposal of record" (`strategies.py:122-124`, docstring `strategies.py:113-115`). Re-generation is a no-op; there is no re-rank path.
- Opportunity rows themselves are produced by the recovery builder (`backend/app/services/recovery/builder.py`) from detected incidents/payments — see FLOW F report for upstream detection. Opportunity types referenced here: `stuck_checkout_payment`, `subscription_halted` (`strategies.py:37` imports `STUCK_CHECKOUT_PAYMENT_TYPE`, `SUBSCRIPTION_HALTED_TYPE` from builder).
- `generate` also backfills the opportunity's planning summary (`expected_recovery_paise`, `confidence`, `risk`) from the recommended candidate (`strategies.py:178-183`) and writes an audit record `recovery.strategies_generated` with the full candidate set (`strategies.py:185-211`).

### G.2 StrategyGenerator candidates

Fixed candidate set of **6**, always generated in this order (`strategies.py:350-406`):

1. `retry_payment` (immediate) — fit from `_RETRY_FIT` table per failure class (`strategies.py:57-64`); eligible only if the linked payment exists AND `status == "failed"` AND failure class is not `HARD_DECLINE` (`strategies.py:307-308`); risk "medium".
2. `retry_payment` (delayed) — same eligibility, `constraints.delay_seconds = 1800` (`DELAY_SECONDS`, `strategies.py:85`); fit = retry fit +0.15 for `INSUFFICIENT_FUNDS` ("payday effect"), −0.08 otherwise (`strategies.py:290-301`); risk "medium".
3. `create_payment_link` — fit from `_LINK_FIT` (`strategies.py:65-72`); eligible if `amount_paise >= 100` (Razorpay payment-link minimum, `strategies.py:48`); risk "low". Special arrears reason for `subscription_halted` (`strategies.py:335-344`).
4. `notify_customer` — fit from `_NOTIFY_FIT` (`strategies.py:73-80`); eligible if a customer row exists and `not customer.opted_out` (`strategies.py:375-388`); risk "low".
5. `escalate_human` — fit 1.0, always eligible, risk "low", constraints `{queue: "human_ops"}` (`strategies.py:390-397`).
6. `no_action` — fit 1.0, always eligible, risk "low", "baseline: do nothing and let organic recovery happen" (`strategies.py:398-405`).

- Failure class input: `STUCK_CHECKOUT_PAYMENT_TYPE` opportunities are forced to `ABANDONMENT` (`strategies.py:239-244`); otherwise `classify_failure(payment)` — substring pattern table over Razorpay error fields with `error_source` fallback (`backend/app/services/revenue/classify.py:31-86`, `132-140`); if no payment, the revenue engine's `opportunity_class_defaults` per opportunity_type (`strategies.py:249-254`, `backend/app/services/revenue/config.py:118-132`).
- Confidence per candidate = `evidence_strength x action_fit` (`strategies.py:166`). `evidence_strength` = latest `Diagnosis.confidence` for the incident when one exists (`strategies.py:256-266`), else `DIAGNOSIS_FREE_EVIDENCE = 0.80` (`strategies.py:45`). Consequence (documented at `strategies.py:13-17`): without an ML diagnosis row, confidence = 0.80 × fit ≤ 0.80 < 0.85 auto-execute floor → every diagnosis-free proposal takes the approval lane. **Auto-execution requires a diagnosis-backed proposal.**

### G.3 Expected-recovery pricing

- Source: `RevenueService.opportunity_estimate(opportunity)` (`backend/app/services/revenue/engine.py:237-317`).
- Formula: `recoverable = amount_paise x recoverability[failure_class]`; `expected[action] = recoverable x strategy_effectiveness[action]` (`engine.py:273-301`). `Estimate.scale` multiplies point and bands by the factor, widening bands outward (floor/ceil) so scaling never falsely narrows (`backend/app/services/revenue/types.py:46-61`).
- Recoverability factors (documented priors, "not a measured fact" — `config.py:1-7`): TIMEOUT 0.70, SOFT_DECLINE 0.60, ABANDONMENT 0.35, INSUFFICIENT_FUNDS 0.20, HARD_DECLINE 0.05, UNKNOWN 0.10 (`config.py:68-89`).
- Strategy effectiveness priors: RETRY_PAYMENT 0.50, CREATE_PAYMENT_LINK 0.30, RESUME_SUBSCRIPTION 0.25, NOTIFY_CUSTOMER 0.15, EXTEND_GRACE_PERIOD 0.10, ESCALATE_HUMAN 0.05, PAUSE_SUBSCRIPTION/REFUND/NO_ACTION exactly 0.0 ("a blocked or protective action never inflates the plan", `config.py:94-113`). Note: effectiveness 0.0 actions are dropped from `expected_recovery_by_strategy` (`engine.py:296-298`) → their `expected_paise` in strategies becomes 0 via `_expected_paise` fallback (`strategies.py:269-274`).
- Single-payment band is the full `[0, amount]`, `low_confidence=True` always, `prior_confidence=0.3` (`engine.py:275-285`, `config.py:60`) — "these numbers rank strategies, they do not promise revenue" (`engine.py:243-247`). **Pricing is prior-based ranking, not a measured recovery probability.**

### G.4 Ranking

- Sort key: eligible-first, then `expected_recovery_paise` desc, then risk asc (`low<medium<high`), then candidate order (`strategies.py:146-154`).
- `selected` = first eligible in that order (`strategies.py:155-157`); persisted per row with `rank` (`strategies.py:159-176`).
- Ranking is fully deterministic arithmetic — no ML in ranking itself (the only learned input is `Diagnosis.confidence` inside evidence_strength, which affects confidence, not rank order).

### G.5 PolicyEngine rules (policies/default.yaml — full rule list)

Every rule in `policies/default.yaml` (version "1.0", verified against loader `backend/app/services/policy/config.py` and engine `backend/app/services/policy/engine.py`):

| # | Rule (yaml key) | Value | Engine enforcement (rule id) |
|---|---|---|---|
| 1 | `kill_switch.enabled` | `false` (exempt: `escalate_human`, `no_action`) | R01 `kill_switch` → BLOCKED all non-exempt (`engine.py:302-305`) |
| 2 | `actions.allowlist` | `retry_payment, create_payment_link, notify_customer, extend_grace_period, pause_subscription, resume_subscription, escalate_human, no_action` — `refund` deliberately absent | R02 `allowlist` → BLOCKED anything unlisted (`engine.py:307-309`) |
| 3 | `auto_execute.min_confidence` | `0.85` | `approval.confidence` → REQUIRES_APPROVAL if below (`engine.py:384-389`) |
| 4 | `auto_execute.max_amount_inr` | `5000` (→500 000 paise) | `approval.amount` → REQUIRES_APPROVAL if above (`engine.py:377-383`) |
| 5 | `auto_execute.max_attempts` | `2` per payment/opportunity | `approval.attempts` → REQUIRES_APPROVAL at attempt ≥2 (`engine.py:390-396`) |
| 6 | `require_human_approval.amount_above_inr` | `5000` | folded into `auto_amount_ceiling_paise = min(...)` (`config.py:147-149`) — stricter-of-two |
| 7 | `require_human_approval.confidence_below` | `0.85` | folded into `auto_confidence_floor = max(...)` (`config.py:152-154`) |
| 8 | `never_auto_execute` | `refund`, `irreversible_action` (metadata flag), `customer_opted_out` (context flag) | R03 → BLOCKED, no approval path (`engine.py:344-363`) |
| 9 | `duplicate_protection.cooldown_minutes` | `60` | R08 `duplicate.cooldown` → BLOCKED same customer+action_type within 60 min unless prior ended REJECTED/CANCELLED/FAILED; RECOVERED and UNKNOWN still block (`engine.py:493-508`, `history.py:30-34`, `125-144`) |
| 10 | `rate_limits.max_actions_per_incident` | `10` | R06 `rate_limit.incident` → BLOCKED (`engine.py:468-477`) |
| 11 | `rate_limits.max_actions_per_customer_per_day` | `3` (UTC day) | R07 `rate_limit.customer_daily` → BLOCKED (`engine.py:479-491`) |
| 12 | `rate_limits.max_actions_global_per_hour` | `100` (rolling hour) | R09 `rate_limit.global_hourly` → BLOCKED (`engine.py:510-518`) |
| 13 | `stopping_rule.max_consecutive_failed_recoveries_per_incident` | `3` | R04 `stopping_rule.incident` → BLOCKED; max(context signal, DB streak) (`engine.py:435-451`) |
| 14 | `stopping_rule.max_consecutive_failed_recoveries_per_strategy` | `3` | R05 `stopping_rule.strategy` → BLOCKED when `metadata.strategy_id` set (`engine.py:453-466`) |
| 15 | `approval.pending_approval_ttl_hours` | **ABSENT from policies/default.yaml** → TTL disabled (optional key, `config.py:106-111`). See H.10. | executor lapse-on-read |

Engine-level rules not in YAML: R00 malformed input → BLOCKED (fail closed; includes currency != INR, `engine.py:154-158`); R10 `separation_of_duties.self_approval` → WARN only, never enforced (`engine.py:311-331`); `safe_action` — `no_action`/`escalate_human` short-circuit to ALLOWED after R00–R02 (`engine.py:333-342`, `SAFE_ACTIONS` at `engine.py:59`); `stateful.unverified` → REQUIRES_APPROVAL when engine has no session/history (`engine.py:397-403`); `internal_error` → BLOCKED on any exception (totality, `engine.py:253-260`).

- Outcome precedence: BLOCKED > REQUIRES_APPROVAL > ALLOWED (`engine.py:414-420`).
- Every decision persisted to `policy_decisions` (flush only); BLOCKED additionally mirrored to `audit_logs` as `policy.action_blocked` (`engine.py:522-558`).
- Rate-limit counts exclude REJECTED/CANCELLED (never consumed budget, `history.py:29`); streak scan limited to last 64 actions (`history.py:37`, `169-184`).
- Loading is strict fail-closed: unknown key/missing section/invalid value → `PolicyConfigError` (`config.py:51-53`, `176-198`); `policy_version = "1.0+sha256.<12>"` from exact file bytes (`config.py:197`). `PolicyEngine.failsafe()` blocks everything with kill switch on (`config.py:201-219`).

### G.6 Approval lanes

- **Entry**: any gate outcome REQUIRES_APPROVAL parks the action in PENDING_APPROVAL (`executor.py:399-411`). Drivers: amount > INR 5 000, confidence < 0.85, attempts ≥ 2, or unverifiable stateful guards (G.5 rules 3–7 + `stateful.unverified`).
- **Queue surfaces**: `GET /api/v1/recovery/opportunities?status=PENDING_APPROVAL` and whole-queue aggregate `GET /api/v1/recovery/opportunities/approvals-summary` (`backend/app/api/v1/recovery.py:388-411`) — feeds the frontend Approval Center.
- **Approve**: `POST /{opportunity_id}/approve` → `executor.approve` stamps APPROVED + `approved_at`/`approved_by` (`executor.py:420-445`); then two additive KYA-lite records: a separation-of-duties re-gate through `PolicyEngine` carrying proposer/approver principals (warn-only, never re-authorizes, `recovery.py:282-323`) and a `recovery.principal_bound` audit row. **Approve does NOT fire the gateway** — response is "approved by human; ready to execute" (`recovery.py:648-650`); a subsequent `execute` sees APPROVED, skips the gate (`executor.py:384`) and fires. Two human steps: approve, then execute.
- **Reject**: `POST /{opportunity_id}/reject` → REJECTED terminal; refused for in-flight/UNKNOWN actions (`executor.py:447-471`). Opportunity-level reject exists when no action was created yet (`executor.py:458-462`).
- **TTL**: `approval.pending_approval_ttl_hours` absent from shipped `policies/default.yaml` → approvals wait indefinitely; lapse-on-read machinery exists but is disabled (H.10).
- **Identity is demo-grade**: principal derives from a shared API key — "binds a cohort, not an individual"; self-approval cannot be excluded and only leaves a `separation_of_duties.self_approval` WARNING on a persisted decision (`recovery.py:21-27`, `282-303`; engine R10 `engine.py:311-331`). No SSO/RBAC: anyone with the API key can approve anything.

### G.7 AI/deterministic boundary

The exact cut (`ports.py:10-11`: "probabilistic AI proposes, deterministic policy decides"; `policies/default.yaml:2-3`: "The policy engine is the ONLY component that may authorize a financial action. Probabilistic components (ML diagnosis, LLM reasoner) only propose."):

AI / probabilistic side (all PROPOSE only):
1. **ML diagnosis** — `Diagnosis.confidence` feeds strategy evidence_strength (`strategies.py:256-267`) and agent reports. Learned model; heuristic fallback exists.
2. **Strategy generator priors** — failure-class fit tables, recoverability/effectiveness priors: documented hand-set numbers, not learned (`strategies.py:53-87`, `config.py:62-113`). Deterministic arithmetic over probabilistic *inputs*.
3. **Reasoner** — `HeuristicReasoner` (default; deterministic rules, `reasoners.py:189-192`) or `LlmReasoner` (optional, `LLM_PROVIDER=openai` + key; bounded 6-iteration tool loop, temperature 0, `reasoners.py:632-790`). Produces `InvestigationReport` — advisory ("ADVISORY ONLY (ADR 0004)", `reasoners.py:1`). **Live deployment: `LLM_PROVIDER=none` — no LLM at all** (`docs/audit/baseline.md:42`, `config.py:39`).

Deterministic side (decides + executes):
4. **`PolicyEngine.evaluate`** — the ONLY authorization path; total, deterministic, explainable, audited (`engine.py:1-21`). Consumes AI output only as *numbers in* `ActionContext` (confidence, amount).
5. **Executor / worker / webhook verification** — pure state machine; no learned component (FLOW H).

Boundary enforcement points (verified in code):
- Agent's `propose_recovery_strategy` tool is a dry-run: "Creates nothing; never executes" (`tools.py:602-653`).
- Agent's `request_payment_link` / `request_recovery_execution` — the ONLY agent mutation path — create PROPOSED rows and gate them: BLOCKED → REJECTED, REQUIRES_APPROVAL → PENDING_APPROVAL; "The gateway is never called from here" (`tools.py:656-657`, `738-806`).
- LLM-specific guardrails: strict JSON validation + hallucination guard (evidence ids, numbers, target ids must come from tool results — `reasoners.py:794-858`, `validation.py`); model confidence capped at an evidence-calibrated ceiling once it approaches the 0.85 floor (`reasoners.py:898-911`); gate confidence capped below the floor for non-auto-recoverable diagnosis classes — measured motivation: "the ML track measured the diagnosis artifact crossing the 0.85 floor on 52.8% of non-auto-recoverable production frames" (`reasoners.py:147-156`); a BLOCKED model recommendation is dropped and replaced with escalate_human (`reasoners.py:951-956`); any LLM failure falls back to the deterministic heuristic reasoner, report marked degraded (`reasoners.py:683-702`).
- Net: **AI ends at the proposal row + confidence number; everything from `PolicyEngine.evaluate` onward is deterministic.** The auto-execute lane additionally requires a diagnosis-backed confidence ≥ 0.85 (G.2) — which is exactly the AI output the gate trusts least, bounded by the caps above.

## FLOW H — Execute → Gateway → Verify → Outcome

- Status: **WORKING** (state machine complete in code; live worker ticking per `docs/audit/baseline.md:31`). Notification delivery is **SIMULATED** in every deployment (H.9).

### H.1 Execute entry points

- `POST /api/v1/recovery/{opportunity_id}/execute` → `RecoveryExecutor.execute()` (`backend/app/api/v1/recovery.py:572-605`; executor `backend/app/services/recovery/executor.py:326-414`). Actor = KYA-lite principal from X-API-Key + declared actor (`recovery.py:582-587`); a `recovery.principal_bound` audit row records the binding (`recovery.py:243-279`).
- `POST /api/v1/recovery/opportunities/build` → `OpportunityBuilder.build_for_incident` + `StrategyGenerator.generate` per created opportunity (`recovery.py:414-435`).
- In-process worker fires due SCHEDULED actions through the same `execute()` path (`backend/app/services/worker/worker.py:144-173`).
- `RecoveryExecutor.execute()` is idempotent find-or-create (`executor.py:334-344`): no open action → create from chosen/recommended strategy; open SCHEDULED not-due → no-op; open UNKNOWN → `resolve()` (re-query, never re-fire); open PENDING_APPROVAL / EXECUTING / VERIFYING → refuse with 409 (`executor.py:349-361`); strategy switch on an open action refused (`executor.py:366-370`).

### H.2 Policy gate at execution

- EVERY execution passes `PolicyEngine.evaluate` first (`executor.py:384-411`, `_gate` at `executor.py:690-741`): context carries amount, confidence, customer opt-out, `attempts_so_far` (count of prior non-REJECTED/CANCELLED actions on the opportunity, `executor.py:743-755`), consecutive failures from DB history, `strategy_id` + `current_action_id` metadata.
- BLOCKED → action REJECTED (terminal) with rules in audit (`executor.py:386-398`); REQUIRES_APPROVAL → PENDING_APPROVAL (`executor.py:399-411`); ALLOWED → `_fire`. APPROVED-by-human actions skip the re-gate (`executor.py:384`).
- SCHEDULED actions that come due are **re-gated fresh at fire time** ("duplicate protection, stopping rules and rate limits re-checked fresh", `executor.py:372-382`).
- Decision record linked on the action (`policy_decision_id`, `executor.py:724-728`); `decided_at` re-stamped each gate (this restarts delayed-retry waits and approval-TTL age honestly, `executor.py:998-1004`).
- Approve endpoint re-runs the gate carrying proposer/approver principals for the SoD warning signal (does not re-authorize, `recovery.py:282-323`).

### H.3 RazorpayGateway methods used

Executor dispatch (`executor.py:850-928`) — exactly one gateway mutation per action type:

| ActionType | Gateway call | Idempotency anchor |
|---|---|---|
| `retry_payment` | `create_order(amount, currency, idempotency_key=gateway_request_id, notes)` (`executor.py:867-882`) | Razorpay `receipt` (order-level dedupe, `client.py:86-93`) |
| `create_payment_link` | `create_payment_link(..., idempotency_key=gateway_request_id)` (`executor.py:883-904`) | Razorpay `reference_id` (`client.py:111-113`) |
| `notify_customer` | **no gateway call** — enqueues outbox row; response `{entity: "notification", outbox_id}` (`executor.py:905-922`) | one outbox row per action |
| anything else (`extend_grace_period`, `pause_subscription`, `resume_subscription`, `refund`, …) | none — raises `GatewayClientError("no executor mapping … only retry_payment, create_payment_link and notify_customer execute")` → FAILED (`executor.py:923-928`) | — |

- `create_subscription` exists on the port + real client + simulator (`ports.py:125`, `client.py:120-141`, `simulated.py:173`) but is **never called by the executor** (grep over `backend/app`: only definition sites + `evaluation/runner.py:627` calling `create_order`). Subscriptions have no gateway-side idempotency; client logs a warning and never retries (`client.py:128-141`).
- Real adapter: raw REST httpx, Basic auth key_id:key_secret (`client.py:40-68`). Mutating POSTs sent **exactly once** (`attempts=1` for non-idempotent, `client.py:169`); exponential backoff only for idempotent GETs on timeout/5xx/429 (`client.py:161-191`).
- Gateway routing by opportunity environment: `research` → injected gateway (simulator twin in every current deployment); `real_test` → real Razorpay adapter or honest `GatewayNotConfiguredError` 409 refusal — "a real_test action never touches the simulator" (`executor.py:142-148`, `180-195`; factory `backend/app/services/razorpay/factory.py:60-84`).

### H.4 UNKNOWN-on-transient handling

- `GatewayTransientError` (timeout / 5xx / unreadable response) → UNKNOWN with `ambiguous_outcome: True`, "never blind-retry" (`executor.py:824-838`, errors mapped in `backend/app/services/razorpay/errors.py`).
- `GatewayClientError` (4xx) → FAILED — "rejected before processing, definitively nothing happened" (`executor.py:813-823`).
- `resolve(action_id)` re-queries gateway truth with GETs only (`executor.py:533-609`): Path 1 `fetch_order(created_id)` for retry actions — RECOVERED iff id matches AND (`status=="paid"` or `amount_paid >= action.amount_paise`); Path 2 `fetch_payment(gateway_payment_id)` of the linked original payment — RECOVERED iff id matches AND captured. **Identity confusion (id mismatch) never recovers — stays UNKNOWN** (`executor.py:566-569`, `584-586`). Inconclusive → audit row `recovery.action.resolve_check`, stays UNKNOWN (`executor.py:591-609`).
- Operator/cron path: `POST /api/v1/recovery/reconcile` and worker unit 3 run `run_reconciliation` — all UNKNOWN actions through `resolve()`, per-unit commits (`backend/app/services/recovery/reconcile.py:59-95`; worker cadence 900 s default, `config.py:59`).

### H.5 Webhook/fetch verification

- Registry `EVENT_HANDLERS` = `payment.captured`, `payment.failed`, `payment_link.paid` only (`backend/app/services/recovery/webhook_handlers.py:233-237`). Other event types are stored with no handler (ack "no handler registered", `webhook_handlers.py:115-116`).
- `payment.captured`: payment → captured (terminal); linked actions in EXECUTING/VERIFYING/**FAILED** → RECOVERED — "late success wins" (`webhook_handlers.py:154-168`, open states `:84-88`).
- `payment.failed`: no-op if payment already captured; else payment → failed with error fields; linked actions in EXECUTING/VERIFYING only → FAILED (`webhook_handlers.py:171-193`, `terminal_ok=False` at `:191`, `:316`).
- `payment_link.paid`: anchored by `reference_id == action.gateway_request_id` (`webhook_handlers.py:208-217`); financial cross-check before RECOVERED — integer `amount` must equal `action.amount_paise` exactly, `currency` must match, partial payments never count (`webhook_handlers.py:394-430`). Mismatch → hold in current state + `verification.amount_mismatch` audit + `last_error`; event still marked processed (`webhook_handlers.py:433-475`). Already-RECOVERED → idempotent no-op (`:220-221`).
- Inline verification: simulator pays links inline → RECOVERED immediately when response shows paid/amount_paid (`executor.py:930-960`); real Razorpay resolves via webhook ("real Razorpay resolves via webhook", docstring `:938-939`). `notify_customer` can never verify inline — waits for the customer's payment webhook (`executor.py:951-952`).
- Ingress (HMAC gate, event-id dedup, raw persistence) is FLOW E's scope; verified live per `docs/audit/baseline.md:41`.

### H.6 Outcome states (RECOVERED/FAILED/UNKNOWN)

Full machine (`executor.py:1-16`, enum `ports.py:54-71`): PROPOSED → POLICY_EVALUATED → ALLOWED→fire / REQUIRES_APPROVAL→PENDING_APPROVAL→(approve→APPROVED→execute | reject→REJECTED | TTL-lapse→PROPOSED) / BLOCKED→REJECTED; delayed retry → SCHEDULED → due → re-gate → fire; EXECUTING → VERIFYING → RECOVERED|FAILED; EXECUTING → UNKNOWN → (resolve/webhook → RECOVERED|FAILED | stays UNKNOWN); cancel (pre-execution) → CANCELLED; escalate (any non-terminal) → ESCALATED. Terminal: RECOVERED, FAILED, REJECTED, CANCELLED, ESCALATED (`executor.py:105-111`). FAILED is **not** terminal for verification — a late `payment.captured` still moves FAILED→RECOVERED (`webhook_handlers.py:87`). UNKNOWN is open, surfaced, counted separately in revenue reports (`backend/app/services/revenue/engine.py:341-346`). Opportunity status shadows latest action (`executor.py:1210-1216`, `recovery.py:95-103`).

### H.7 Duplicate-execute invariant + row locks

- **One gateway mutation per action, ever**: `gateway_request_id` (unique column, `gwr_` id ≤36 chars, `executor.py:662-664`) is the idempotency key mapped to Razorpay `receipt`/`reference_id`.
- **One open action per opportunity**: second `execute` on same opportunity reuses the open action instead of creating a new one (`executor.py:346-370`); OPEN_STATES include UNKNOWN and SCHEDULED — "a SCHEDULED action still holds the execution slot" (`executor.py:84-94`, `:23-25`).
- **Row lock**: `SELECT ... FOR UPDATE` on the opportunity row at the top of execute/approve/reject/escalate/cancel (`executor.py:207-220`, used at `:346`, `:429`, `:455`, `:485`, `:511`) — Postgres serializes concurrent executors; **silently omitted on SQLite** (docstring `:208-212`) — local dev relies on SQLite writer serialization.
- Cross-opportunity duplicates: policy R08 `duplicate.cooldown` (60 min, same customer+type; RECOVERED/UNKNOWN still block, G.5 rule 9).
- In-flight (EXECUTING/VERIFYING) → 409 refuse; UNKNOWN → resolve-not-retry (`executor.py:349-356`).
- Gateway-side reality (`client.py:6-11`): orders dedupe via `receipt`, payment_links via `reference_id`; **payments/subscriptions have NO Razorpay idempotency** — protection is the internal ledger + never-retry-on-transient.

### H.8 SCHEDULED delayed retry + worker firing

- Parking: `_fire` with `constraints.delay_seconds > 0` and not due → SCHEDULED, **no attempt consumed, nothing reaches the gateway**, stays cancellable (`executor.py:770-781`, `_park :1022-1039`). Due anchor = `decided_at` (re-gate restarts the wait) else `proposed_at` (`executor.py:998-1011`). Only delayed-retry strategies carry `delay_seconds=1800` (G.2 candidate 2).
- Firing: worker unit 1 scans `status == SCHEDULED` ordered by created_at, checks `scheduled_due(now)`, calls `executor.execute(opportunity_id, actor="system:worker")`, commits per row; one bad action never aborts the unit (`worker.py:144-173`). Fire goes through the normal path: row lock, open-action reuse, **policy re-gate**, then gateway (`executor.py:372-413`). Leaving SCHEDULED means a later tick never re-fires (`worker.py:8-10`).
- Loop: `WorkerSupervisor` paces `tick()` every `WORKER_TICK_SECONDS` (default 30.0, `config.py:57`) via `asyncio.to_thread`; failure-isolated units; liveness stamped per tick (`supervisor.py:62-79`). `WORKER_ENABLED` default **False** (`config.py:56`) — live Render deployment has it `true` and worker ok/ticking (`docs/audit/baseline.md:31`).
- Single-process design: one worker instance; the opportunity row lock is the cross-writer guard on Postgres (`worker.py:26-28`).

### H.9 Notify outbox

- Enqueue: `notify_customer` execution inserts `notification_outbox` row (PENDING, due now, payload carries customer + message "your payment did not complete — please retry at your convenience", `executor.py:1041-1111`); one row per action ("the action fires exactly once, ever", `:1049-1051`).
- Delivery: worker unit 2 delivers due PENDING rows via the `NotificationSender` port, per-row commit; failure → linear backoff 60 s×attempt, max 3 attempts, then FAILED — "surfaced, never silently dropped" (`worker.py:179-250`, `:50-51`).
- **Delivery is SIMULATED in all deployments**: default `LoggingNotificationSender` only logs and returns `{via: "logging"}` (`backend/app/services/worker/senders.py:29-50`); the `razorpay_notes` seam "performs no external delivery today" and returns `simulated: true` (`senders.py:53-75`). `WORKER_NOTIFICATION_SENDER` default `logging` (`config.py:62`). No SMS/email/provider integration exists. The customer is never actually contacted — recovery via notify depends on a notification nobody receives. Recovery verification for notify actions waits on the customer's payment webhook (H.5).

### H.10 Approval TTL

- Optional rule `approval.pending_approval_ttl_hours` (≥1 h) — **ABSENT from `policies/default.yaml`** → TTL disabled in the shipped config: PENDING_APPROVAL actions wait for explicit approve/reject indefinitely (`config.py:106-111`, `executor.py:241-248` — docstring "the shipped default — lapse disabled").
- Mechanism when enabled: lapse-on-read inside `open_action_for` — PENDING_APPROVAL older than TTL (age from `decided_at`/`proposed_at`) returns to PROPOSED with a `PolicyDecisionRecord` (rule `approval.pending_approval_ttl_hours`, actor `system:approval_ttl`) and must be re-gated + re-approved (`executor.py:234-238`, `250-305`). At exactly the TTL the approval is still live (`:253-254`).
- No background sweeper for approvals: lapse happens only when the opportunity is next read/acted on ("lapse-on-read").

### H.11 What can/cannot run on the audit Razorpay test account

Account facts from `docs/audit/baseline.md:40-41`: reads on orders/payments/subscriptions/payment_links OK; writes OK on orders + payment_links; **401 on subscriptions and direct-payments APIs** (products not enabled); webhook intake verified live (HMAC pass stored, bad signature 400).

CAN run end-to-end on the audit account:
- `retry_payment` execution → `POST /orders` (allowed). Verification via `payment.captured` webhook or `fetch_order`/`fetch_payment` GETs (allowed). Note: Razorpay has no "retry" call — a fresh order is created; someone must still pay it (`executor.py:867-870`). In Test Mode a payment against the order can be created only via Razorpay's own checkout/test-card flow, not by this app (direct `POST /payments` is 401 AND unused by the code).
- `create_payment_link` execution → `POST /payment_links` (allowed). Verification via `payment_link.paid` webhook (verified live) or resolve GETs. Test-mode links are payable through Razorpay's hosted page.
- UNKNOWN → resolve() (GET-only, allowed) and the reconcile sweep (operator + worker cadence).
- SCHEDULED delayed retry + worker firing (in-process; gateway call is `POST /orders` when due).
- Policy gate, approvals, row locks, audit — all environment-independent.

CANNOT run on the audit account:
- `create_subscription` → `POST /subscriptions` returns **401** (products not enabled). Moot for recovery: the executor never calls it (H.3). Subscription-linked recovery strategies (`pause/resume_subscription`, `extend_grace_period`) have **no executor mapping at all** → they'd die as FAILED "no executor mapping" even on a fully-enabled account.
- Actual customer notification delivery — SIMULATED in every environment (H.9); not an account limitation but a product gap.
- Starvation caveat (verified context): detection consumes the webhook-driven payment EVENT stream, which real_test barely has — so although the H machinery works against the test account, upstream opportunities from live traffic are scarce (6 real payments synced).

## FLOW I — Evaluation

- Status: **WORKING as a SIMULATED measurement harness** — the pipeline under test is real production code; the environment, customer, and operator are deterministic simulations. Recovery-effect numbers are **prior-driven by construction** (I.5).

### I.1 Scenario → simulator → ground truth

- 5 named scenarios (`backend/app/simulator/config.py:182-187`): `standard` (30 days, ~65k events, one incident of each kind), `quiet`, `upi_outage_demo`, `payday_wave_demo`, `storm` (8 overlapping incidents). POST `/api/v1/evaluation/run` accepts scenario/seed/days/events/customers/holdout_fraction overrides (`backend/app/api/v1/evaluation.py:118-143`, runner `backend/app/services/evaluation/runner.py:399-435`).
- Each arm calls `run_simulation(config, db)` into its **own scratch SQLite tempfile DB** — the main/demo DB is never touched (`runner.py:5-7`, `_ScratchDb :343-361`). Deterministic run id = `sim_{seed}_{config_hash[:10]}` (`config.py:122-125`); dataset version pinned as `run_id@anchor_date` (`runner.py:221-225`).
- Ground truth: the simulator writes `simulator_ground_truth` rows (entity_type incident|payment|subscription; truth JSON with kind/start/end/recoverable/affected_amount) alongside `simulator_runs` (`backend/app/simulator/engine.py:928`, `:972`, `:983-1001`; model `backend/app/models/evaluation.py:87-106`). The runner loads incident truth and maps `IncidentKind → diagnosis CauseLabel` (`runner.py:191-205`, `305-327`).

### I.2 Detect → diagnose → recover inside evaluation

Two arms, same seed/config (`runner.py:512-513`):

- **BASELINE arm** (`runner.py:612-651`): for EVERY failed payment — one `gateway.create_order` on the simulator twin (idempotency key `baseline:{payment_id}`), then a conversion draw from `CONVERSION[cls]["immediate_retry"]`. No detection, no diagnosis, no policy gate, no verification (`ungated_actions_count = interventions_count`). Hard-decline retries counted as false interventions.
- **PULSECOVER arm** (`runner.py:657-712`) — the real loop, unchanged:
  1. **Detect**: scheduled `run_detection` passes every 360 min looking back 720 min, production detector defaults, research environment (`runner.py:180-185`, `747-775`). Scored vs ground truth by window overlap → precision/recall/F1; MTTD in simulator time (`runner.py:776-810`).
  2. **Diagnose**: real `DiagnosisService.classify` per first-detected incident per ground-truth row → top-1/top-3 accuracy (`runner.py:814-868`).
  3. **Recover**: real `OpportunityBuilder` (or `HoldoutExcludingBuilder`), `StrategyGenerator.generate`, `RecoveryExecutor.execute` — policy gate with decisions counted; PENDING_APPROVAL → harness operator `human:eval_operator` approves, then re-executes (`runner.py:872-920`).
  4. **Verify**: the simulated customer answers VERIFYING actions per the `CONVERSION` table, and the capture is delivered as a **signed simulator webhook through the real `EVENT_HANDLERS` registry** (dedup on gateway_event_id included) (`runner.py:1380-1455`). Payment links are decided inline by the simulator twin (`GATEWAY_SUCCESS_RATE = 0.35`, `runner.py:109-112`).
- Reproducibility: `_deterministic_ids` guard replaces uuid4 ids with a deterministic counter inside arm phases; conversion draws seeded on stable simulator identities — "two runs with the same seed reproduce identical metrics (wall-clock MTTR excepted)" (`runner.py:16-25`, `375-393`).
- Safety invariant counted per run: an action reaching EXECUTING+ only with an ALLOWED decision or recorded approval; violations counted in `unsafe_action_count` (refund also counts) (`runner.py:35-37`, `941-953`).

### I.3 Metrics + holdout lift

- Assembled metrics (`runner.py:554-606`): detection precision/recall/F1/MTTD, diagnosis top1/top3, recovery_rate (verified recovered ÷ failed amount), recovered_revenue_paise, interventions + false_interventions per arm, false_action_rate, unsafe_action_count, MTTR minutes (**wall-clock** pipeline latency proposed_at→verified_at — labeled an operational measurement, `runner.py:40-43`, `962-965`), arm comparison deltas/ratios.
- **Holdout** (pre-registered, `backend/app/services/evaluation/holdout.py:1-21`): deterministic customer-level assignment `sha256('holdout:{seed}:{customer_id}')[:8]/2^64 < fraction` (default 0.10, `holdout.py:35`, `47-64`); held-out customers get detection+diagnosis but NO opportunities/actions (`HoldoutExcludingBuilder`, `holdout.py:122-160`); payments without a customer id stay in treatment (disclosed, `holdout.py:58-61`). Estimand: ITT lift = rate(treatment) − rate(holdout), denominators = ALL first-attempt failed payments snapshotted BEFORE any action (`runner.py:288-302`, `714-743`); both groups share an organic `no_action` self-resolution baseline (uniform 0–7d lag, right-censored at scenario end, captured through the real webhook path, `runner.py:175-178`, `1275-1311`). CI: Newcombe hybrid score/Wilson 95% (`holdout.py:89-103`); secondary class-adjusted lift via pooled-weight post-stratification (`runner.py:1239-1273`); per-stratum lift by failure class and method; isolation counters (`holdout_opportunities_count`, `holdout_actions_count`) asserted 0 (`runner.py:1101-1126`, `1212-1215`).
- Persistence: one `evaluation_runs` row (status running|completed|failed; failures persisted honestly with the exception in notes, `runner.py:538-546`) + one `experiments` row with the full config, dataset anchor/version, diagnosis-artifact and policy versions (`runner.py:439-508`; models `backend/app/models/evaluation.py:16-66`).
- API: `POST /api/v1/evaluation/run` **synchronous** ("roughly a minute or two" at standard preset, `evaluation.py:1-9`); `GET /runs`, `GET /runs/{id}` serve stored rows only — "they never compute metrics on the fly" (`evaluation.py:9`, `46-75`); `GET /metrics` = means/sums over completed stored runs (`evaluation.py:78-115`).

### I.4 Evaluation Lab (frontend)

- Route `/evaluation` → `EvaluationView` (`frontend/src/app/evaluation/page.tsx`, `frontend/src/components/evaluation/evaluation-view.tsx`); also embedded as a tab in the Research Lab (`evaluation-view.tsx:120-131`).
- Stored-row viewer: "the console never recomputes metrics" (`evaluation-run-detail.tsx:283`); runs table + detail stack (arm comparison, holdout lift chips with CI tone, detection/diagnosis/recovery/intervention bars, per-incident diagnosis table, "Methodology & honest caveats" section) (`evaluation-run-detail.tsx:91-275`, `evaluation-holdout.tsx:152-243`).
- Trigger: POST run; because the harness is synchronous and can outlive the 120 s client timeout, a timeout is treated as "run continuing on server" — the page polls the stored row every 4 s until it appears (`evaluation-view.tsx:71-88`, `146-153`).
- **Live state (verified 2026-09-02T08:45Z)**: exactly one stored run exists in production (`console-2026-09-02-04-18`, standard scenario, status completed, duration ~8m42s — `GET /api/v1/evaluation/runs?page_size=3`); `GET /api/v1/evaluation/metrics` aggregates over it (detection P 0.6 / R 0.833 / F1 0.698, diagnosis top-1 0.8 / top-3 1.0, MTTD 419 min, unsafe_action_count 0). The frontend parser reads `policy_outcomes` (`evaluation-metrics.ts:63,239`) but **not** `opportunity_types` (I.6).

### I.5 Does it measure recovery or a proxy?

**It measures the real machinery on a simulated world; per-action recovery magnitudes are priors, and the one live stored run shows the fleet-level result is dominated by coverage, not by those priors.**

- Genuinely measured (code behavior, not priors): detection P/R/F1 + MTTD vs injected truth; diagnosis top-1/top-3 vs injected cause; policy outcome distribution; approvals required; interventions count; false interventions (hard declines touched); `unsafe_action_count` invariant; wall-clock MTTR; holdout isolation counters; end-to-end integrity of executor + webhook verification (captures flow through the real signed-webhook handlers, `runner.py:1426-1455`).
- Prior-driven (circular per-action): every recovered rupee comes from the hand-set `CONVERSION` table — "a documented prior, not a measurement" (`runner.py:126-129`). Per touched payment the table favors acting over not acting (action columns ≥ `no_action`, hard declines excepted, `runner.py:124-128`). The priors also rhyme with the planner's strategy-effectiveness priors (`config.py:94-113`) — internally consistent, externally unvalidated.
- **But fleet-level recovery is NOT guaranteed by the prior ordering — live proof.** The single stored production run (fetched live 2026-09-02T08:45Z from `GET /api/v1/evaluation/runs/run_7544714073134d408020a52910d7528f`, run `console-2026-09-02-04-18`, standard scenario, completed 04:27Z):
  - Baseline: 4 893 failed payments (367 166 100 paise), 4 893 interventions, recovered **99 011 600 paise (26.97%)**, 433 false interventions.
  - PulseCover: 1 289 opportunities (`opportunity_types`: 940 failed_payment_retry, 309 stuck_checkout_payment, 40 subscription_halted), policy outcomes **BLOCKED 1 189 / REQUIRES_APPROVAL 100 / ALLOWED 0** — the auto-execute lane never fired; 100 approvals (harness operator approves all), 100 interventions (90 retry + 10 link), recovered **1 674 400 paise (0.46%)** — 1.69% of baseline's haul. `unsafe_action_count` 0, UNKNOWN 0, false_action_rate 0.1.
  - Holdout: treatment 609/4 444 (13.70%; 28 via action + 581 organic) vs holdout 73/449 (16.26%, all organic) → **lift −2.55 pp, 95% CI [−6.38, +0.74]**; class-adjusted −1.37 pp [−4.65, +1.91]. Isolation counters 0/0 (holdout truly untouched).
  - Consequence: the run's own pre-registered hypothesis ("Policy-gated, diagnosis-driven recovery recovers more revenue with orders-of-magnitude fewer interventions", `runner.py:467-470`) is confirmed on interventions (98% reduction) and **falsified on revenue** (1.7% of baseline). The lift CI includes 0 → "no measurable incremental recovery" is the honest read; the point estimate is negative.
  - Why BLOCKED dominates is not recoverable from the stored metrics (per-action rules lived in the deleted scratch DB) — UNCERTAIN. Likely contributors (inference, labeled as such): the batch harness gates all 1 289 actions within one wall-clock burst, so the time-windowed guards (60-min duplicate cooldown per customer+type, 3 actions/customer/UTC day, stopping rules) fire far more often than they would on organically spaced traffic. The 0-ALLOWED result, however, is structural: see G.2 — without a diagnosis ≥0.85 evidence the confidence math cannot reach the auto-execute floor, and REQUIRES_APPROVAL (not ALLOWED) is the ceiling for nearly everything.
- No live-traffic recovery measurement exists anywhere: production `actual_recovered` sums webhook-verified actions (`engine.py:319-365`), but with SIMULATED notification delivery (H.9) and near-zero real_test event flow (live opportunities list is EMPTY — `GET /api/v1/recovery/opportunities` → `{"items":[],"total":0}` fetched 2026-09-02T08:45Z), real recovered volume is not exercisable in the audit deployment.
- Disclosed in-band: harness notes and the UI methodology section state the simulated roles and prior table plainly (`runner.py:16-43`, `1216-1236`; `evaluation-holdout.tsx:216-239`).

### I.6 opportunity_types persistence

- Persisted: the PulseCover arm groups `recovery_opportunities` by `opportunity_type` and stores the dict in run metrics under `arms.pulsecover.opportunity_types` — "persisted in the run metrics so it is machine-checkable instead of derived during run analysis. Additive: older runs simply lack the key" (`runner.py:968-984`).
- Covered by a backend test: `backend/tests/evaluation/test_opportunity_types.py:17-28` asserts the breakdown is persisted.
- **NOT surfaced**: no reference to `opportunity_types`/`opportunityTypes` anywhere in `frontend/src` (grep, 0 matches) and it is not part of the `GET /api/v1/evaluation/metrics` aggregate (`evaluation.py:86-115`) — it is only visible inside the raw stored metrics JSON of a run detail.

## Cross-cutting findings (severity-tagged)

Live evidence cited below was fetched 2026-09-02T08:45–08:47Z from `https://pulserecover-api.onrender.com` (light GET probes only).

1. **[HIGH] The auto-execute lane is structurally dead.** In the only stored production evaluation run, the gate decided 1 289 actions: **ALLOWED 0**, REQUIRES_APPROVAL 100, BLOCKED 1 189 (live run metrics, I.5). Code-side this is by construction: diagnosis-free confidence is capped at 0.80 < 0.85 floor (`strategies.py:45`), and with a diagnosis the LLM/heuristic confidence rarely clears 0.85 either (the `_gate_confidence` cap exists because the artifact crossed the floor on 52.8% of non-auto-recoverable frames, `reasoners.py:147-156`). Every recovery in production therefore needs a human click — "autonomous recovery" is, today, "assisted recovery". That is arguably the safe posture, but it contradicts any claim of auto-execution working in practice.
2. **[HIGH] Customer notification delivery is SIMULATED in every environment.** `LoggingNotificationSender` logs only; `razorpay_notes` returns `simulated: true` and "performs no external delivery today" (`senders.py:29-75`). `notify_customer` actions enqueue, get marked SENT, and nobody is contacted. Recovery attribution for notify actions depends on a customer who never saw anything.
3. **[HIGH] The evaluation's headline revenue comparison inverts the product thesis.** The stored run: naive retry-everything baseline recovered 27.0% of failed amount; the full gated loop recovered 0.46% (1.69% of baseline) with 98% fewer interventions; holdout lift −2.55 pp (CI includes 0). What the harness genuinely proves is safety (0 unsafe actions, 0 UNKNOWN, holdout isolation 0/0) — not recovery efficacy (I.5).
4. **[MEDIUM] 4 of 8 allowlisted action types have no execution path.** `extend_grace_period`, `pause_subscription`, `resume_subscription` are allowlisted by policy and carry effectiveness priors, but the executor raises "no executor mapping" → FAILED for anything except retry_payment / create_payment_link / notify_customer (`executor.py:923-928`). A strategy recommending them would burn an attempt to discover this. (They are never generated as candidates by the current StrategyGenerator — G.2's fixed six — so the gap is latent, not live.)
5. **[MEDIUM] Approval identity is demo-grade.** Shared API key → cohort principal; self-approval cannot be excluded; the separation-of-duties check is WARN-only (`recovery.py:238-323`, `engine.py:311-331`). Anyone with the one API key can approve and execute any action, including above-₹5 000 ones.
6. **[MEDIUM] Approval TTL machinery shipped but disabled.** `approval.pending_approval_ttl_hours` absent from `policies/default.yaml` → PENDING_APPROVAL waits forever; lapse is read-triggered only (`executor.py:241-305`, `config.py:106-111`). Live policy hash matches the repo file (`1.0+sha256.5a6afe61d6db` live = local `sha256sum` prefix — verified), so this is the production behavior.
7. **[MEDIUM] Production recovery pipeline is starved, not broken.** Live `GET /api/v1/recovery/opportunities` (real_test) → 0 items; approvals-summary → 0 pending (2026-09-02T08:45Z). Worker healthy (tick 7 s ago at probe time), gateway `razorpay_test`, policy engine ok (`/api/v1/system/health`). The machinery works; the webhook-driven event stream that feeds detection has ~no real traffic (6 synced payments per baseline).
8. **[LOW] Duplicate-execute protection has a SQLite gap.** The opportunity row lock (`SELECT ... FOR UPDATE`) is silently omitted on SQLite (`executor.py:207-220`) — fine for local dev, but the compose stack and Render run Postgres, so the invariant holds in deployed environments only.
9. **[LOW] `opportunity_types` persisted but invisible.** Stored in run metrics (`runner.py:968-984`, test `tests/evaluation/test_opportunity_types.py:17-28`), absent from the `/evaluation/metrics` aggregate and the entire frontend (`evaluation-metrics.ts` has no field for it; grep 0 matches in `frontend/src`).
10. **[LOW] Evaluation MTTR is a wall-clock batch artifact (disclosed).** Live run reports MTTR 0.0025 min because the harness executes synchronously at scenario end; honestly labeled in code and notes (`runner.py:40-43`, `1089-1092`).

Positive confirmations (no severity): one-gateway-mutation-per-action invariant enforced by unique `gateway_request_id` + open-action reuse + row lock (H.7); UNKNOWN-on-transient with GET-only resolution and identity-confusion guards (H.4); `payment_link.paid` amount/currency/partial cross-check before RECOVERED (H.5); policy decisions persisted with content-hashed policy version and BLOCKED mirrored to audit (G.5); live policy file byte-identical to repo `policies/default.yaml`; worker, webhook intake, reconcile sweep all real code paths exercised by the harness (I.2).

## Unverifiable items

- **Which policy rules fired for the 1 189 BLOCKED decisions** in the stored run — per-action decisions lived in the deleted scratch DB; only aggregate counts persist. The batch-burst explanation (time-windowed guards firing because 1 289 gates run in one wall-clock minute) is INFERENCE, not established fact.
- **Diagnosis confidence distribution** behind the 100 REQUIRES_APPROVAL outcomes (confidence vs amount driver) — same scratch-DB limitation.
- **Whether the demo frontend's Approval Center has ever held a real_test pending action in production** — current queue is empty; no historical read API for lapsed/approved queues.
- **SimulatedPaymentGateway inline link behavior** (`simulated.py:123-171`) — read but not deeply traced for this report; relied on executor's `_verify_inline` + twin docstrings.
- **Razorpay-side receipt/reference_id dedupe behavior** — code maps the keys correctly (`client.py:86-93`, `111-113`), but actual gateway dedupe was not re-probed live by this agent (docs/research.md claims verified there; not re-checked).

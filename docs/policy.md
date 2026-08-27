# PulseRecover — Policy Engine & Audit Trail

The policy engine (`app/services/policy/`) is the deterministic gate that every
proposed financial action must pass before execution (ADR 0003). It implements
`app.ports.PolicyEngineProto`:

```python
from app.services.policy import PolicyEngine, load_policy_config

config = load_policy_config()                 # strict; PolicyConfigError on a bad file
engine = PolicyEngine(config, session=db)     # or PolicyEngine.from_file(session=db)
decision = engine.evaluate(action_context)    # never raises; always a PolicyDecision
```

> **Probabilistic AI proposes. Deterministic policy decides.** The ML
> diagnosis and the LLM reasoner only ever produce a `StrategyCandidate` /
> `ActionContext`. They have no execution path — this module is the single
> choke point between a proposal and the payment gateway.

---

## 1. Threat model

### Assets at stake

- **Money**: recovery actions move real funds (retries, payment links,
  subscription changes) through Razorpay test mode (and, in principle, live).
- **Customer trust & compliance**: unwanted customer contact (opted-out
  customers) and double-charging are regulatory/reputational events, not bugs.
- **Runaway automation**: an unbounded retry loop can trip card-network
  resubmission limits (~15/30 days) and incur scheme penalties.

### Adversaries and failure modes

| # | Threat | Example | Mitigation (rule id) |
|---|--------|---------|----------------------|
| T1 | **Hallucinated / manipulated AI output.** The LLM proposes an action that is flatly prohibited. | Prompt-injected context makes the reasoner recommend `refund` to an attacker-controlled account. | Closed action allowlist + hard blocks (`allowlist`, `never_auto_execute.*`). AI output is only ever an *input* to the gate. |
| T2 | **Overconfident autonomy.** Probabilistic confidence is treated as ground truth. | Diagnosis emits 0.91 confidence on a noisy signal and retries a ₹40,000 payment. | Auto-execute ceilings (`approval.amount`, `approval.confidence`, `approval.attempts`) → `REQUIRES_APPROVAL`. |
| T3 | **Runaway loop.** A bug or adversarial input keeps re-proposing actions. | A permanently failing payment is retried forever, burning network attempt budgets. | Stopping rules (`stopping_rule.incident`, `stopping_rule.strategy`), rate limits (`rate_limit.*`). |
| T4 | **Duplicate execution.** The same proposal is evaluated twice (retry, race, double-click). | Two payment links for the same customer within a minute → double collection. | Duplicate protection (`duplicate.cooldown`); gateway idempotency is layered on top by the executor. |
| T5 | **Contacting opted-out customers.** | A "dunning" notification to a customer who withdrew consent. | `never_auto_execute.customer_opted_out` — hard block, no approval lane. |
| T6 | **Malformed / adversarial input.** Garbage types, NaN confidence, foreign currency, negative amounts. | `confidence=NaN` slips past a `<` comparison (NaN comparisons are all false). | Input normalization with fail-closed validation (`malformed.*` → BLOCKED). NaN/out-of-range can never pass. |
| T7 | **Config tampering / operator error.** | A typo (`min_confidance:`) silently drops the confidence floor. | Strict loader: unknown keys, missing sections, and invalid values raise `PolicyConfigError` — the process refuses to start. Content-hash `policy_version` ties every decision to the exact file bytes. |
| T8 | **Emergency stop needed.** | A bad deploy starts blocking legitimate recovery; ops needs one switch. | `kill_switch` blocks everything except the non-financial escape hatches. |
| T9 | **History unavailable / DB degraded.** | A preview path without a session could "evaluate" without stateful guards. | Without a history source the best possible outcome is `REQUIRES_APPROVAL` (`stateful.unverified`) — auto-execution is structurally impossible. |
| T10 | **Audit gap.** | A blocked action leaves no trace; "false-action rate" becomes unmeasurable. | Every decision is persisted to `policy_decisions`; every BLOCKED decision is mirrored to the append-only `audit_logs`. |

### Trust boundary

```
strategy generator / reasoner (UNTRUSTED, probabilistic)
        │  ActionContext
        ▼
┌─────────────────────────────┐
│ PolicyEngine.evaluate()     │  ← TRUSTED, deterministic, no LLM, no network
│  normalize → rules → decide │
└─────────────────────────────┘
        │  PolicyDecision (+ persisted policy_decisions row)
        ▼
recovery executor (TRUSTED) — executes ONLY on ALLOWED,
routes to a human on REQUIRES_APPROVAL, never on BLOCKED
```

The engine deliberately does **not** do API authentication/authorization
(that is the `X-API-Key` middleware's job) or fraud scoring (that is an
upstream signal). It is a pure, inspectable rule set — small enough to audit
by reading, which is the point.

### Design guarantees

- **Total:** `evaluate()` never raises. Any internal error, broken history, or
  garbage input returns `BLOCKED` (`internal_error` / `malformed.*`).
- **Deterministic:** same `(ActionContext, PolicyConfig, history state)` →
  same `(outcome, reasons, rules_matched, policy_version)`. No randomness, no
  LLM, no wall-clock influence on the *outcome* (`decided_at` is only a
  timestamp). Rule order is fixed; every matched rule is recorded.
- **Monotone conservative:** when `auto_execute` and `require_human_approval`
  disagree, the engine applies the **stricter** of the two bounds
  (`auto_amount_ceiling_paise`, `auto_confidence_floor`). A config slip can
  only tighten the gate, never loosen it.
- **Fail closed:** unknown action types, malformed fields, broken YAML,
  missing history — all resolve to BLOCKED / no-auto-execute.
- **Outcome precedence:** `BLOCKED` > `REQUIRES_APPROVAL` > `ALLOWED`. A rule
  that blocks always wins over rules that merely approve-gate.

---

## 2. Evaluation pipeline (rule reference)

Rules evaluate in this fixed order; all matching rules are recorded in
`rules_matched` with human-readable counterparts in `reasons`.

| Order | Rule id | Condition | Effect |
|-------|---------|-----------|--------|
| R00 | `malformed.action_type` | action type not a known `ActionType` value | BLOCKED |
| R00 | `malformed.amount` | amount not an integer, or negative | BLOCKED |
| R00 | `malformed.confidence` | confidence not a number, NaN/inf, outside [0,1] | BLOCKED |
| R00 | `malformed.currency` | currency ≠ `INR` (thresholds are INR) | BLOCKED |
| R01 | `kill_switch` | `kill_switch.enabled` and action not exempt | BLOCKED |
| R02 | `allowlist` | action type not on `actions.allowlist` | BLOCKED |
| — | `safe_action` | `no_action` / `escalate_human` with no prior block | ALLOWED (short-circuit; these move no money and contact no customer — they are the escape hatch) |
| R03 | `never_auto_execute.<action>` | action listed in `never_auto_execute` (e.g. `refund`) | BLOCKED — no approval lane exists |
| R03 | `never_auto_execute.irreversible_action` | `metadata["irreversible_action"]` truthy | BLOCKED |
| R03 | `never_auto_execute.customer_opted_out` | `ctx.customer_opted_out` | BLOCKED |
| R04 | `stopping_rule.incident` | `max(ctx.consecutive_failures, DB streak)` ≥ limit | BLOCKED |
| R05 | `stopping_rule.strategy` | DB streak for `metadata["strategy_id"]` ≥ limit | BLOCKED |
| R06 | `rate_limit.incident` | budget-consuming actions on the incident ≥ limit | BLOCKED |
| R07 | `rate_limit.customer_daily` | actions for the customer in the current UTC day ≥ limit | BLOCKED |
| R08 | `duplicate.cooldown` | active same-type action for the customer within the cooldown | BLOCKED |
| R09 | `rate_limit.global_hourly` | actions globally in the rolling hour ≥ limit | BLOCKED |
| R10 | `approval.amount` | amount > effective ceiling (₹5000 default) | REQUIRES_APPROVAL |
| R11 | `approval.confidence` | confidence < effective floor (0.85 default) | REQUIRES_APPROVAL |
| R12 | `approval.attempts` | `attempts_so_far` ≥ `auto_execute.max_attempts` (2) | REQUIRES_APPROVAL |
| R13 | `stateful.unverified` | no history source was available | REQUIRES_APPROVAL (auto-execution impossible) |
| — | `auto_execute.ok` | nothing above matched | ALLOWED |
| — | `internal_error` | an exception escaped the pipeline | BLOCKED |

Notes on the stateful guards:

- **Budget-consuming** means every status except `REJECTED`/`CANCELLED` —
  proposals that never reached the gateway do not consume rate-limit budget.
- **Duplicate protection** treats an action as active unless it conclusively
  ended (`REJECTED`/`CANCELLED`/`FAILED`). `RECOVERED` and `UNKNOWN` still
  block: never double-collect, never re-fire an action whose outcome is
  unclear.
- **Self-exclusion:** if the caller has already persisted the action under
  evaluation (as `PROPOSED`), it must pass `metadata["current_action_id"]` so
  the guards never count the action against itself.
- The per-incident stopping rule takes the **higher** of the caller-supplied
  `consecutive_failures` and the streak recorded in the database — the caller
  cannot talk the gate below the recorded reality.

### `ActionContext.metadata` keys consumed by the engine

| Key | Type | Effect |
|-----|------|--------|
| `strategy_id` | str | enables the per-strategy stopping rule (R05) |
| `current_action_id` | str | excluded from every history query; also stored on the `policy_decisions` row |
| `irreversible_action` | truthy | hard block when `irreversible_action` is in `never_auto_execute` |
| `request_id` | str | propagated to the `audit_logs` row for BLOCKED decisions |

---

## 3. Configuration reference (`policies/default.yaml`)

Loading is strict (Pydantic, `extra="forbid"`): unknown keys, missing
required sections, out-of-range values, and YAML syntax errors all raise
`PolicyConfigError` at load time. **Fail closed: no config, no gate.**

`policy_version` is `"<version>+sha256.<first 12 hex of file hash>"` — any
edit, even a comment, changes the version recorded on every decision.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `version` | str | *(required)* | Human-readable config version; part of `policy_version`. |
| `kill_switch.enabled` | bool | `false` | When true, BLOCK everything except `exempt_actions`. |
| `kill_switch.reason` | str | `""` | Recorded in the block reason (e.g. "incident response"). |
| `kill_switch.exempt_actions` | list[str] | `[escalate_human, no_action]` | Actions that stay allowed under the kill switch. |
| `actions.allowlist` | list[str] | *(required)* | Closed set of authorizable `ActionType` values. `refund` is deliberately absent. |
| `auto_execute.min_confidence` | float [0,1] | `0.85` | Auto-execution requires confidence ≥ this. |
| `auto_execute.max_amount_inr` | int ≥ 0 | `5000` | Auto-execution amount ceiling, **INR** (×100 → paise at load). |
| `auto_execute.max_attempts` | int ≥ 0 | `2` | Auto-execution only while `attempts_so_far` < this. |
| `require_human_approval.amount_above_inr` | int ≥ 0 | `5000` | Above this → human approval. The engine applies the *stricter* of this and `auto_execute.max_amount_inr`. |
| `require_human_approval.confidence_below` | float [0,1] | `0.85` | Below this → human approval. Stricter-of-two with `min_confidence`. |
| `never_auto_execute` | list[str] | *(required)* | Hard blocks: `ActionType` values (e.g. `refund`) and/or the flags `irreversible_action`, `customer_opted_out`. Unknown entries are rejected at load. |
| `duplicate_protection.cooldown_minutes` | int ≥ 0 | `60` | Same customer + action type inside this window → BLOCKED. |
| `rate_limits.max_actions_per_incident` | int ≥ 0 | `10` | Budget-consuming actions per incident. |
| `rate_limits.max_actions_per_customer_per_day` | int ≥ 0 | `3` | Per customer per UTC day. |
| `rate_limits.max_actions_global_per_hour` | int ≥ 0 | `100` | Rolling-hour global brake. |
| `stopping_rule.max_consecutive_failed_recoveries_per_incident` | int ≥ 1 | `3` | Consecutive FAILED streak per incident halts automation. |
| `stopping_rule.max_consecutive_failed_recoveries_per_strategy` | int ≥ 1 | `3` | Same, keyed by `metadata["strategy_id"]`. |

Required sections: `version`, `actions`, `never_auto_execute`,
`auto_execute`, `require_human_approval`, `stopping_rule`, `rate_limits`.
Optional (safe defaults): `kill_switch`, `duplicate_protection`.

Possible future rule (not implemented): an approval TTL
(`approval.pending_approval_ttl_hours`) under which actions awaiting
approval longer than the TTL lapse back to PROPOSED review. No such lapse
worker exists today — PENDING_APPROVAL actions wait for an explicit
approve/reject.

### Money convention

Thresholds are written in INR for readability and converted to integer paise
at load (`max_amount_inr: 5000` → `500000` paise). Comparisons are strict:
exactly ₹5000.00 is *within* the ceiling; ₹5000.01 is above it. Non-INR
contexts are BLOCKED — the gate never converts currencies.

### Failsafe mode

If the policy file cannot be loaded but an engine object is still required
(e.g. a health endpoint), `PolicyEngine.failsafe(reason)` returns an engine
whose kill switch blocks **everything** (`policy_version="failsafe"`). The
normal startup path should instead treat `PolicyConfigError` as fatal.

---

## 4. Persistence contract

With a session attached, `evaluate()`:

1. writes one immutable `policy_decisions` row per evaluation — outcome,
   reasons, rules matched, policy version, actor, full normalized context,
   `decided_at`;
2. additionally mirrors every **BLOCKED** decision into `audit_logs`
   (`action="policy.action_blocked"`) — blocked proposals are
   security-relevant and feed the false-action-rate metric.

Both writes are **flushed, never committed**: the caller owns the transaction
boundary. (Rollback discards the decision row together with the caller's
unit of work — see flags in the integration notes if a stronger
commit-independent audit trail is ever required.)

Without a session the engine still evaluates — but only context-only rules
can run, so the best possible outcome is `REQUIRES_APPROVAL`
(`stateful.unverified`). Preview mode can never self-authorize an execution.

---

## 5. Audit helper contract (`app/services/policy/audit.py`)

A small, generic helper over the append-only `audit_logs` table — import it
from anywhere:

```python
from app.services.policy import audit

audit.record(
    session,
    actor="human:ops@example.com",      # who did it (str, truncated to 128)
    action="recovery.approve",          # dotted verb, <domain>.<verb>
    entity_type="recovery_action",      # table-ish noun
    entity_id=action.id,
    details={"note": "looks safe"},     # any dict; coerced to JSON-safe
    request_id=None,                    # optional; falls back to the
)                                       # RequestIdMiddleware contextvar
```

Contract:

- **Append-only.** There is deliberately no update/delete helper. Rows are
  immutable; corrections are new rows.
- **Flush, never commit.** `record()` flushes so `entry.id` is usable
  immediately; the caller's transaction boundary is respected.
- **Never crashes the audited operation.** Non-serializable `details` values
  become strings; oversized strings are truncated to column width.
- **`created_at` is set by the helper** (tz-aware UTC) — the column has no
  default.
- Query path: `GET /api/v1/audit` (see `app/schemas/audit.py`), indexed by
  `(entity_type, entity_id)` and `created_at`.

---

## 6. Integration notes for other modules

- The recovery executor must call `evaluate()` for **every** proposed action
  and must treat only `ALLOWED` as executable; `REQUIRES_APPROVAL` routes to
  the approval flow, `BLOCKED` is terminal for that proposal.
- Pass `metadata={"current_action_id": action.id, "strategy_id": strategy.id,
  "request_id": request_id}` when the action row already exists — this keeps
  the guards accurate and links the decision row to the action.
- `PolicyDecisionRecord.action_id` is a deliberate soft reference (no FK);
  join on it freely but do not rely on referential integrity.
- Tests: `backend/tests/policy/` — 81 tests covering every rule, the
  fail-closed paths, determinism, persistence, and the audit helper.

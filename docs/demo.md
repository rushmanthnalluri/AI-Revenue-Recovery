# PulseRecover — Demo Guide (hiring panel, 5 minutes per scenario)

Five deterministic, resettable end-to-end scenarios, driven by one script
against a scratch sqlite DB that is reset on every run. Nothing is mocked and
nothing is forced: every number below is copied from a real, reproduced run
(`tests/demo` re-runs each scenario twice and asserts identical key numbers).

> Probabilistic AI proposes. Deterministic policy decides. Payment
> infrastructure executes. Verification proves.

## Running it

From `backend/`:

```bash
python scripts/demo_run.py --scenario A --db scripts/.demo_A.db   # one scenario
python scripts/demo_run.py --scenario all --db scripts/.demo.db   # all five (~2 min)
python -m pytest tests/demo -v                                    # the proof suite (~2.5 min)
```

`--db` is a scratch file the script deletes and recreates; the app database is
never touched. Each scenario seeds the simulator with a fixed seed AND a fixed
end date (2026-08-16), then drives the real pipeline over HTTP via an
in-process FastAPI TestClient:

```
seed -> POST /api/v1/detection/run -> POST /api/v1/incidents/{id}/investigate
     -> POST /api/v1/recovery/opportunities/build -> GET .../plan
     -> POST .../execute (| approve) -> signed simulator webhook -> audit trail
```

The gateway is `SimulatedPaymentGateway` — the same `PaymentGateway` port the
Razorpay test-mode adapter implements; webhooks are genuinely HMAC-signed and
verified. Printed entity ids (`inc_...`, `opp_...`) are uuid4 by design and
differ between runs; every *number* is identical between runs.

---

## Scenario A — Major degradation, full closed loop

**What it proves:** the whole loop on a realistically-sized incident — a tuned
2.5-hour gateway degradation over a 2-day, ~144k-event dataset.

**Narrative:** "It's 2pm IST and the gateway is degrading. Detection anchors
just after the incident window and fires: success rate fell **82.9% -> 69.0%
(-16.7%)**, 1,359 failed payments, **Rs 10,36,582 at risk**. The ML diagnosis
correctly reads `gateway_degradation` (confidence 0.8997) and the AI
investigator explains it; the revenue engine turns it into 1,715 per-payment
opportunities. We then execute three recoveries: the Rs 8,047 soft-decline
retry is **above the Rs 5,000 auto-execute ceiling (and its 0.8097 confidence
is below the 0.85 floor), so the deterministic gate holds it for a human**;
the two timeout retries (Rs 504, Rs 509) carry 0.8817 confidence — genuinely
above the floor — and auto-execute. Every recovery is proven by a signed
`payment.captured` webhook, and every state change is in the append-only
audit trail."

Expected output (real, reproduced — this exact text, modulo ids):

```
[DETECT] POST /api/v1/detection/run - metric=payment_success_rate, window=240m, bucket=10m, anchored 2026-08-15 11:25 UTC
        anomaly -> incident inc_...: success rate 82.9% -> 69.0% (-16.69%), severity=MEDIUM
        blast radius: 1359 failed payments, Rs 10,36,582 at risk
        ground truth (answer key): kind=gateway_degradation, affected=476, injected_failures=476, expected_cause=gateway_outage
[DIAGNOSE] ML root-cause: gateway_degradation (confidence 0.8997, model diagnosis-logistic_regression@v20260826T234303Z-c5434878)
[QUANTIFY] POST /api/v1/recovery/opportunities/build -> 1715 per-payment opportunities (Rs 13,02,241 of failed payments in scope)
[POLICY] gate: REQUIRES_APPROVAL (rules: approval.amount, approval.confidence) - Rs 8,047 is above the Rs 5,000 auto-execute ceiling; confidence is below the 0.85 auto-execute floor; routing to a human
[VERIFY] webhook payment.captured (HMAC signature valid, event id deduped) -> action RECOVERED - Rs 8,047 recovered
[POLICY] gate: ALLOWED (rules: auto_execute.ok) - auto-execute lane (<= Rs 5,000, confidence >= 0.85)
[VERIFY] webhook payment.captured ... -> action RECOVERED - Rs 504 / Rs 509 recovered
[RESULT] 3/3 executions verified RECOVERED - Rs 9,060 of Rs 10,36,582 at risk recovered in this run
```

The scenario is deliberately tuned so the auto-execute lane is *earned*: at
fail_boost 0.12 the diagnosis is confident enough (0.8997) that timeout-retry
strategies reach 0.8997 x 0.98 = 0.8817 confidence — over the 0.85 floor on
merit, while the milder 0.10 variant (0.8599 -> 0.8427) would honestly take
the approval lane instead. The policy boundary is never weakened to make the
demo pass.

## Scenario B — Safe autonomous recovery

**What it proves:** low value + diagnosis-backed confidence >= 0.85 ->
fully automatic execution, verified by webhook. No human in the loop.

**Narrative:** "A Rs 501 card payment timed out before authorization. The
strategy engine ranks an immediate retry highest (confidence 0.98 — the
diagnosis is certain and timeouts are the most recoverable class). The gate
evaluates the real policy file: under Rs 5,000, confidence above 0.85,
attempts under 2 — `auto_execute.ok`, ALLOWED, fired immediately. The
webhook proves the capture."

```
[STRATEGIZE] GET plan -> recommended: retry_payment (expected recovery Rs 175, confidence 0.9800, risk medium); policy preview: ALLOWED
[POLICY] gate: ALLOWED (rules: auto_execute.ok) - auto-execute lane (<= Rs 5,000, confidence >= 0.85)
[VERIFY] webhook payment.captured (HMAC signature valid, event id deduped) -> action RECOVERED - Rs 501 recovered
[RESULT] no human touched this run: policy ALLOWED, executed, and the webhook verified Rs 501 RECOVERED
```

## Scenario C — Human approval lane

**What it proves:** above Rs 5,000 the gate refuses autonomy; a human approves;
execution proceeds on the recorded decision; webhook verifies.

**Narrative:** "Same degradation shape, but this payment is Rs 10,143. The AI
proposes the same retry — the gate says REQUIRES_APPROVAL (`approval.amount`).
It sits in PENDING_APPROVAL until `human:ops` approves. Only then does the
executor fire — once — and the webhook moves it to RECOVERED."

```
[POLICY] gate: REQUIRES_APPROVAL (rules: approval.amount) - Rs 10,143 is above the Rs 5,000 auto-execute ceiling; routing to a human
[APPROVE] human:ops approved -> APPROVED; executing on the recorded decision
[VERIFY] webhook payment.captured ... -> action RECOVERED - Rs 10,143 recovered
```

## Scenario D — Gateway timeout: UNKNOWN, no blind retry, truthful resolve

**What it proves:** the hardest honesty property. When the mutating call gets
no authoritative answer, the system does NOT guess and does NOT re-fire.

**Narrative:** "The gateway 503s mid-execution. The action lands in UNKNOWN —
'executed but outcome unverifiable'. Automation pauses: the operator's
re-execute performs a GET-only re-query of the same action instead of a second
mutation — total mutating calls stays at **1**. When the gateway recovers and
its own records show the payment captured, the same re-query path resolves the
action to RECOVERED on evidence, never on hope."

```
[EXECUTE] gateway timed out on the mutating call -> action UNKNOWN (GatewayTransientError: simulated gateway outage)
[RE-EXECUTE] operator retries the execute - the executor must NOT re-fire the mutation
        same action act_... re-queried instead of re-fired (gateway mutations attempted: 1 total)
[RESOLVE] GET-only re-query (fetch_payment) proves the capture -> action RECOVERED
[RESULT] timeout -> UNKNOWN -> paused -> resolved RECOVERED on gateway evidence; exactly 1 mutating call was ever attempted
```

## Scenario E — Unsafe AI recommendation blocked

**What it proves:** the deterministic gate is the only execution path. A
manipulated or hallucinating AI that proposes a **refund** (never on the
allowlist, explicitly in `never_auto_execute`) is stopped cold — with zero
gateway calls.

**Narrative:** "We plant a compromised AI output: refund Rs 543, confidence
0.99. Confidence is not authority. The gate matches `allowlist` +
`never_auto_execute.refund`, the action ends REJECTED, and the gateway
mutation counter reads **0**. The block itself is audited."

```
[ATTACK] a manipulated AI run proposes action_type=refund for Rs 543 (confidence 0.99 - confidence is not authority)
[POLICY] gate: BLOCKED (rules: allowlist, never_auto_execute.refund) - refund is not on the allowlist and is in never_auto_execute; there is no approval lane
[RESULT] action REJECTED - blocked by the deterministic policy gate. Gateway mutations attempted: 0 (zero money moved).
```

---

## Determinism & tests

`backend/tests/demo/test_demo_scenarios.py` runs each scenario twice against
fresh scratch DBs with the exact CLI configs:

- terminal-state assertions per scenario (A: 3x RECOVERED with the approval
  lane on the large one and >= Rs 8L at risk; B: auto-ALLOWED -> RECOVERED,
  confidence >= 0.85, <= Rs 5,000; C: REQUIRES_APPROVAL -> approve ->
  RECOVERED; D: UNKNOWN -> UNKNOWN -> RECOVERED with exactly 1 mutation;
  E: BLOCKED/REJECTED with 0 mutations);
- determinism: the two runs' key numbers (detection values, blast radius,
  diagnosis label/confidence, policy outcomes, rules matched, terminal
  statuses, recovered amounts, mutation counts) must be identical — entity
  ids are projected out (uuid4 by design).

Verified 2026-08-26 against the retrained diagnosis artifact
(`v20260826T234303Z-c5434878`): **10 passed in 150s / 145s on two consecutive
runs**. The full backend suite was re-verified 2026-08-27: **415 passed**
(`cd backend && .venv/Scripts/python -m pytest tests -q`).

## Where the pieces live

- `backend/scripts/demo_run.py` — the runner: scenario configs, scratch-DB
  harness, narrative printer, CLI. `--json` also dumps the machine-readable
  key numbers.
- `backend/tests/demo/` — the proof suite above.
- Seeding goes through `app.simulator.engine.run_simulation` directly (the
  `/api/v1/demo` router is a separate UI-facing surface and is not used here).

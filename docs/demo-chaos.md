# PulseRecover — Demo Failure Chaos Runbook

Nine deliberate ways to break the demo environment in front of a panel, what
the system provably does in each, and how to recover. The point is not to
avoid failure — it is to **fail visibly but safely**, with the failure itself
becoming evidence for the design: honest health, typed errors, no blind
retries, deterministic fallbacks, human-in-the-loop degradation.

Every observation below was produced by a real run on **2026-08-28** against
the current HEAD (same code the demo container builds): backend uvicorn +
frontend dev server, seeded with the canonical `upi_outage_demo` scenario
(41,354 rows; incident `inc_1e645c47bb3441d09316d10f62c8ec53`; same-day
deterministic figures identical to `docs/demo-script.md` Appendix B).
UI observations were captured as real screenshots from a scripted headless
Chromium driving the actual console; backend observations are pasted command
output, structured logs, and DB state. Nothing here is mocked, and **no
product code was changed** to produce any of it.

- Demo-day stack (per `docs/demo-script.md`): compose on ports **8100**
  (backend) / **3200** (console) / **55432** (Postgres). The induction
  commands below are written for that stack.
- Verification environment used for the evidence pasted here: local processes
  (uvicorn :8200, Next dev :3300, scratch SQLite) — same build, same behavior;
  differences are called out where they matter (chaos 3 and 6).
- Companion: `docs/demo-script.md` §Appendix C (short panel-facing version).

**Ground rules for live chaos:** never touch ports 3000/8000 (host
processes) or 8001/3100 (e2e suite); always restore what you broke (artifact
pointer, env, containers); the demo DB is disposable — `POST
/api/v1/demo/reset` + re-trigger the scenario rebuilds it deterministically
in ~20s.

---

## The chaos matrix

| # | Failure injected | Panel sees | Backend truth | Recovery |
|---|---|---|---|---|
| 1 | Backend process killed mid-demo | Red `Backend unreachable` panels with **Retry**, `API · Offline` pill; page chrome intact; self-heals on poll after restart | Process down; `curl` exit 7; zero state lost (DB untouched) | Restart backend; panels refetch on their poll cadence (health 20s, summary 15s, slowest 60s) or click Retry |
| 2 | Real mode + wrong Razorpay keys | Action card **FAILED** "gateway definitively rejected (nothing happened)"; no crash, no 500 | Health honestly `gateway: razorpay_test`; typed `GatewayAuthenticationError`; 1 gateway call, definitive 4xx | Flip `SIMULATION_MODE=true` (or set real test keys); with keys *missing* the factory falls back to the simulator and health says so |
| 3 | Postgres unreachable | **Without `connect_timeout`:** every DB endpoint hangs; UI shows 10s `timeout` panels, `API · Connecting`. **With `?connect_timeout=3`:** `/readyz` 200 in ~3s with `database: down`; health card red **down** pill | `OperationalError` per check; `/healthz` stays 200 (correct liveness); policy/gateway checks stay green | Restart Postgres / fix `DATABASE_URL`; state intact. See **Finding F1/F2** before demoing this live |
| 4 | Forged webhook signature | Nothing (attacker-visible surface is API-only) | HTTP **400** `invalid_webhook_signature` (also 400 on missing header); WARNING log with request id; **zero rows written** | None needed — fail-closed by construction; show `webhook_events` count unchanged |
| 5 | Out-of-order webhooks (`failed`→`captured`, then late duplicate `failed`) | Opportunity pill moves VERIFYING → FAILED → **RECOVERED**; late `failed` changes nothing | Captured is terminal; late success wins over earlier failure; every transition audited; all three events acked `processed:true` | None needed — the state machine *is* the recovery story |
| 6 | Gateway 500/timeout on execute (real HTTP adapter) | Action **UNKNOWN**: "gateway outcome ambiguous — no blind retry, resolution by re-query" | `GatewayServerError` → UNKNOWN; exactly **1 mutating POST** ever; re-execute does GET-only re-query (3 backed-off GETs), stays UNKNOWN while gateway keeps 500ing | Flip gateway back to healthy → same execute re-queries and resolves (chaos 7) |
| 7 | Recovery action stuck UNKNOWN | Approval center → **"Needs resolution (1)"**; amber UNKNOWN + `NEEDS RESOLUTION` badge, `last_error` in red mono, **"Re-query gateway truth"** button | `POST .../execute` on an UNKNOWN action short-circuits to `resolve()` (GETs only); RECOVERED only on positive gateway evidence (`fetch_payment` captured) | Click the button (or POST execute); audit shows `unknown → resolve_check → recovered`; `attempts` stays 1 |
| 8 | LLM returns garbage | Investigation panel: blue `LLM reasoner · gpt-4o-mini (fallback: heuristic)` + red **DEGRADED** badge with the reason; report content still complete | 2 bounded attempts → schema/JSON validation rejects → deterministic heuristic fallback; `degraded:true` + `degraded_reasons`; HTTP 200, no failed run | None needed live — this *is* the fallback. Remove the bad LLM config to return to clean LLM mode |
| 9 | Diagnosis model artifact missing | Incident card: amber **HEURISTIC FALLBACK** badge `diagnosis-heuristic@heuristic-1`, confidence ≤0.7; investigation escalates to a human | Rule-based fallback labels the incident (here `no_fault` 0.45 vs ML `method_outage` 0.9787); report uncertainty: "any automation takes the human-approval lane" | Restore `artifacts/diagnosis_active.json`; next fresh diagnosis is the ML model again (verified: bit-identical 0.978722) |

---

## 1. Backend unavailable mid-demo

**Induce (demo-day):** `docker compose -f deploy/docker-compose.yml stop backend`
(restart with `... start backend`). Verified equivalent locally: kill the
uvicorn process mid-session.

**Observed UI (verified, screenshots):** within one poll cycle of the kill
(health poll 20s, summary 15s) the console renders designed error states —
no white screen, chrome/nav intact, cached demo-control list still visible:

- `Dashboard summary unavailable — The PulseRecover API is not responding.
  Start the backend (uvicorn on localhost:8200) and retry.` — `CODE
  unreachable`, **Retry** button (forces an immediate refetch).
- Same `Backend unreachable` panel in the timeseries section; `Health check
  failed` inside the System Health card.
- Sidebar pill flips green `API · LIVE` → red pulsing **`API · OFFLINE`**.
- After restart the pill flips back to `API · LIVE` and panels self-heal on
  their own poll cadence (observed: summary recovered at ~15–20s; the 60s
  timeseries panel still showed its stale error panel at that moment — a
  Retry click clears it instantly; see F3).

**Observed backend (verified):**

```text
$ curl -sS --max-time 4 http://127.0.0.1:8200/healthz
curl: (7) Failed to connect to 127.0.0.1 port 8200 ... Could not connect to server
$ netstat -ano | grep ":8200 " | grep LISTENING || echo "nothing listening"
port 8200: nothing listening (backend dead)
# after restart: {"status":"ok"} — all prior state (incidents, actions, audit) intact
```

**Recovery:** start the backend; nothing else. SQLite/Postgres state is
untouched by a process kill; in-flight HTTP requests from the moment of the
kill simply fail and are retried by the console's polling.

**Talk-track:** "Watch — the console knows exactly what happened and tells
the operator how to fix it. No phantom data, no spinner of death: every
panel fails closed with a retry, and the second the API is back the console
heals itself. The backend is stateless; the truth is in the database."

## 2. Razorpay credentials wrong or missing in real mode

**Induce:** run real mode with dummy keys — compose override or `.env`:
`SIMULATION_MODE=false`, `RAZORPAY_KEY_ID=rzp_test_dummy`,
`RAZORPAY_KEY_SECRET=dummy`, `RAZORPAY_BASE_URL=https://api.razorpay.com/v1`.
Then execute any approved recovery action.

> Verification note (honesty): this sandbox has **no internet egress**
> (a direct curl to `api.razorpay.com` timed out), so the 401 below was
> served by a local stub reproducing Razorpay's error-envelope shape over
> real HTTP through the real `RazorpayGateway` httpx adapter — the code path
> under test (401 → typed `GatewayAuthenticationError` → FAILED) is
> identical. The live-against-real-Razorpay run of this exact beat is
> recorded in `docs/demo-script.md` Appendix A (2026-08-27).

**Observed backend (verified, pasted):**

```text
GET /api/v1/system/health -> checks.gateway = {"status":"ok","detail":"razorpay_test"},
                             simulation_mode = false        # adapter initialised; no crash
POST /api/v1/recovery/opp_.../execute (after human approve) ->
  status: "FAILED"
  message: "gateway definitively rejected the execution (nothing happened)"
action last_error: "GatewayAuthenticationError: Authentication failed (chaos stub: invalid API key)"
action attempts: 1
stub access log: [{"method":"POST","path":"/v1/orders"}]      # one call, no retry on a definitive 4xx
```

The process keeps serving normally afterwards (policy gate, other actions,
health all fine) — a misconfigured credential degrades one action, never the
app.

**Missing-keys variant (verified):** `SIMULATION_MODE=false` with
`RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` empty →
`GET /api/v1/system/health` reports `gateway: {"status":"ok","detail":"simulator"}`.
The factory falls back to the simulation twin — **the app can never silently
hit the network without keys** (`app/services/razorpay/factory.py`).

**Recovery:** set valid test-mode keys (Appendix A of the demo script) or
flip `SIMULATION_MODE=true` and restart.

**Talk-track:** "Wrong keys don't crash us and don't hang us: the first real
call fails as a *typed* authentication error, the action is marked FAILED —
which truthfully means 'nothing happened at the gateway' — and health was
already telling you which mode you were really in."

## 3. Database connectivity lost

**Induce (demo-day):** `docker compose -f deploy/docker-compose.yml stop db`.
Induce (local): boot the backend with
`DATABASE_URL="postgresql+psycopg://chaos:chaos@127.0.0.1:59999/pulserecover"`
(nothing listening). Use the `postgresql+psycopg://` dialect — a bare
`postgresql://` URL fails at import with `ModuleNotFoundError: psycopg2`
(this stack ships psycopg v3; compose already uses `+psycopg`).

**Observed — two materially different behaviors, both verified:**

**(a) Default URL (no `connect_timeout`) — the dangerous one.** psycopg
retries the refused connection *forever* (libpq default `connect_timeout=0`;
measured still hanging at 35s while a raw socket to the same port is refused
in ~2s). Every DB-touching endpoint (`/readyz`, `/api/v1/system/health`, all
data routes) blocks indefinitely; `/healthz` keeps answering 200 in ~30ms
(correct liveness). The console's 10s client timeout is the only thing that
surfaces anything: panels show `The API did not respond within 10 seconds`
(`CODE timeout`), the System Health card sits in its loading skeleton, and
the sidebar pill shows grey `API · CONNECTING`. Verified screenshot + curl:

```text
GET /healthz                  -> 200 {"status":"ok"}            (0.031s)
GET /readyz                   -> curl (28) timed out after 15s  (hangs)
GET /api/v1/system/health     -> curl (28) timed out after 15s  (hangs)
```

**(b) URL with `?connect_timeout=3` — the designed honest degradation.**

```text
GET /readyz -> 200 in 3.01s
  {"status":"ok", ..., "checks":{"database":{"status":"down","detail":"OperationalError"}}}
GET /api/v1/system/health -> 200 in 3.03s
  database down; policy_engine ok (1.0+sha256.5a6afe61d6db); gateway ok (simulator); llm disabled
```

Console System Health card (verified screenshot): red **`down`** pill on the
database row with the `OperationalError` detail; other components green.

**Recovery:** `docker compose -f deploy/docker-compose.yml start db` (or fix
`DATABASE_URL`); pre-ping re-connects on the next request — no backend
restart needed, no state lost.

**Talk-track:** "Liveness and readiness are different questions here: the
process stays alive and tells you exactly which component is down, in red,
with the driver error attached. And notice the console never invented data —
it waits, times out, and offers retry."

> **Findings F1/F2 below apply to this scenario — read before demoing it
> live.** With the stock compose `DATABASE_URL` (no `connect_timeout`),
> `stop db` presents as hanging endpoints (behavior a), not the clean red
> pill (behavior b).

## 4. Invalid webhook signature

**Induce (any stack):**

```bash
curl -X POST http://localhost:8100/webhooks/razorpay \
  -H 'Content-Type: application/json' \
  -H 'X-Razorpay-Signature: deadbeefdeadbeef' \
  -H 'x-razorpay-event-id: evt_chaos_bad_sig' \
  -d '{"entity":"event","event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_x"}}}}'
```

**Observed backend (verified, pasted):**

```text
HTTP 400 {"error":{"code":"invalid_webhook_signature","message":"Invalid webhook signature",
                   "request_id":"cd7989ee09aa45338b0b9c500a2451de"}}
# missing header variant -> HTTP 400 "Missing X-Razorpay-Signature header"
# backend log:
{"level":"WARNING","logger":"app.api.v1.webhooks","msg":"webhook signature mismatch",
 "request_id":"cd7989ee09aa45338b0b9c500a2451de"}        # same request id as the 400
# DB side-effect check (before -> after):  webhook_events=0 -> 0,
# payments=9405 -> 9405, payment_events=21135 -> 21135   (zero side effects)
```

**Observed UI:** none — the webhook surface is machine-to-machine; the panel
sees the curl and the unchanged dashboard.

**Recovery:** none needed — verification fails closed *before* parsing, so a
forged payload never becomes state. The HMAC is computed over the raw body
with the configured webhook secret (`RAZORPAY_WEBHOOK_SECRET`, or the
simulator's dev secret).

**Talk-track:** "The signature gate runs before any JSON parsing — a forged
capture is a 400 and a warning log with a request id, and I can show you the
row counts: it touched nothing."

## 5. Out-of-order webhooks

Razorpay delivers at-least-once, **unordered**. The state machine must (and
does) make *late truth win* without ever regressing a terminal state.

**Induce (verified sequence; needs one VERIFYING action — execute any small
approved opportunity first):** send three genuinely-signed events for the
same payment, in this order: `payment.failed`, `payment.captured`, then a
late duplicate `payment.failed`. Sign with HMAC-SHA256 over the raw JSON
body using the webhook secret; give each a unique `x-razorpay-event-id`.
(On the compose stack the `captured` beat is also packaged as
`docker compose exec backend python scripts/demo_live.py captured
--opportunity-id <opp_id>`.)

**Observed (verified, pasted — action `act_9880c1dda0c54d99986c193c8d3c6b98`,
₹100 UPI payment):**

```text
1) payment.failed  -> HTTP 200 processed:true   action VERIFYING -> FAILED (last_error: upi_timeout)
2) payment.captured-> HTTP 200 processed:true   action FAILED -> RECOVERED (verified_at set)
   payment row: status failed -> captured; payment_events gains
   ('payment.captured','failed','captured', source='webhook')
3) late payment.failed -> HTTP 200 processed:true  action STAYS RECOVERED; payment STAYS captured
   (captured is terminal; the late failure is a no-op, still acked + stored)
webhook_events rows: 3 (every delivery persisted with its processed flag)
# backend log, transitions with trigger:
VERIFYING -> FAILED  (trigger: payment.failed)
FAILED -> RECOVERED  (trigger: payment.captured)
```

**Observed UI:** the opportunity's status pill walks VERIFYING → FAILED →
RECOVERED on the next poll (15–30s); the audit trail shows each transition
with `system:webhook` as the actor.

**Recovery:** none — this scenario *is* the recovery proof.

**Talk-track:** "Webhooks arrive whenever they arrive. A failure notice
after a success can't claw the money back, and a success after a failure
still recovers it — the state machine resolves by event truth, not arrival
order, and every hop is in the audit log."

## 6. Malformed gateway responses on execute (500s / timeout)

**Induce (demo-day, rehearsed):** `docker compose -f deploy/docker-compose.yml
exec backend python scripts/demo_live.py beat-d --incident-id <incident_id>`
— the demo script's Beat D (simulated outage → `GatewayTransientError`).

**Induce (verified here — harsher, real HTTP adapter):** real mode
(`SIMULATION_MODE=false` + keys) with `RAZORPAY_BASE_URL` pointed at a stub
that answers every call with a Razorpay-shaped **500** envelope, then
execute an approved ₹100 opportunity. This drives the production
`RazorpayGateway` httpx code: status mapping, single-fire mutations,
backed-off GET retries.

**Observed (verified, pasted):**

```text
POST /api/v1/recovery/opp_.../execute ->
  status: "UNKNOWN"
  message: "gateway outcome ambiguous; marked UNKNOWN — no blind retry, resolution by re-query
            (GatewayServerError: chaos stub: downstream 500, outcome ambiguous)"    (~0.3s)
stub access log: [{"method":"POST","path":"/v1/orders"}]            # sent ONCE, never retried

# operator presses execute again:
same action_id, still UNKNOWN ("resolve re-query inconclusive ...")
stub access log: 1x POST /v1/orders + 3x GET /v1/payments/pay_...   # re-query is GET-only,
                                                                    # idempotent GETs back off (0.25s, 0.5s)
action attempts: 1                                                  # mutation counter unmoved
```

**Observed UI:** the action appears in the Approval center's **Needs
resolution** queue within one 15s poll (see chaos 7).

**Recovery:** chaos 7 — make the gateway truthful again and re-query.

**Talk-track:** "A 500 after 'charge the customer' is the scariest sentence
in payments: maybe it happened. PulseRecover never guesses — one mutation,
ever, then it reads gateway truth with idempotent GETs. Watch the mutation
counter: it stays at 1 no matter how many times I press execute."

## 7. Recovery action stuck UNKNOWN → needs resolution → resolved on evidence

**Induce:** chaos 6 leaves the action UNKNOWN. (The queue lives at
`/recovery` → **Approval center**.)

**Observed UI (verified screenshot):** metric strip `Needs resolution · 1 ·
ambiguous gateway outcomes (UNKNOWN)`; card: ⚠ **Retry payment** + pulsing
amber `UNKNOWN` pill + amber `NEEDS RESOLUTION` badge, amount ₹100, the
explanation *"UNKNOWN means the gateway never gave an authoritative answer.
Resolve by re-querying truth; never by re-firing the charge."*, the raw
`last_error` in red mono, and a **"Re-query gateway truth"** button (which
simply POSTs `.../execute` — the executor short-circuits UNKNOWN → read-only
`resolve()`).

**Observed backend (verified, pasted):** after flipping the stub to "truth"
(its records now show the payment captured):

```text
POST /api/v1/recovery/opp_.../execute -> status: "RECOVERED" ("executed and verified — revenue recovered")
stub access log (whole life of the action): 1x POST /v1/orders, 4x GET /v1/payments/... — never a 2nd mutation
action attempts: 1, verified_at set
audit trail: recovery.action.unknown (EXECUTING->UNKNOWN, ambiguous_outcome:true)
             recovery.action.resolve_check (result: still_unknown)
             recovery.action.recovered (UNKNOWN->RECOVERED, verification: fetch_payment)
```

Note the discipline: `resolve()` marks RECOVERED **only on positive gateway
evidence** (order paid / linked payment captured). While the stub kept
500ing, the re-query recorded `still_unknown` and left the action surfaced.

**Recovery:** this *is* the recovery procedure for any ambiguous gateway
outcome: re-query, never re-fire. If evidence never materializes, escalate
to a human from the same card (ESCALATED is terminal for automation).

**Talk-track:** "This is the money-safety beat. The system would rather
admit 'I don't know' in amber than be wrong about your revenue — and it
climbs out of 'don't know' using only reads, on evidence, audited."

## 8. Malformed AI response (scripted LLM garbage)

**Induce (verified):** configure the optional LLM reasoner at a garbage
responder — `LLM_PROVIDER=openai`, `OPENAI_API_KEY=dummy`,
`OPENAI_BASE_URL` pointing at a stub whose `/chat/completions` returns a
well-formed chat.completion envelope whose `message.content` is *"GARBAGE
{{{ definitely not the investigation JSON schema"*. (An unreachable
`OPENAI_BASE_URL` exercises the same fallback via the transport-error path.)
Then (re-)run an investigation:
`POST /api/v1/incidents/{id}/investigate {"force_refresh": true}`.

**Observed backend (verified, pasted):**

```text
status: completed                                    # NOT a failed run, NOT a 500
reasoner: "llm", generated_by: "gpt-4o-mini (fallback: heuristic)"
degraded: true
degraded_reasons:
  - "llm investigation failed after 2 attempt(s): unparseable llm output:
     no JSON object found in LLM response"
  - "fell back to the deterministic heuristic reasoner"
stub access log: 2x POST /chat/completions           # bounded attempts, then fallback
report content: complete deterministic evidence report (tool facts, hypotheses, gated next step)
```

**Observed UI (verified screenshot):** investigation panel badges: blue
**`LLM reasoner · GPT-4O-MINI (FALLBACK: HEURISTIC)`** + red **`⚠ DEGRADED`**,
followed by "Degraded because: llm investigation failed after 2 attempt(s):
unparseable llm output…; fell back to the deterministic heuristic
reasoner." The report body underneath is the full evidence-based report.

**Recovery:** none needed mid-demo — the degradation is the safe behavior.
Fix/remove the LLM config and re-run with `force_refresh` to get a clean
report.

**Talk-track:** "The AI is advisory and untrusted by design. Garbage in is
schema-validated, retried once, then quarantined — the system tells you it
degraded, in red, and keeps working from deterministic evidence. The policy
gate never saw the garbage at all."

## 9. Diagnosis model artifact unavailable

**Induce (demo-day):** `docker compose -f deploy/docker-compose.yml exec
backend mv /srv/artifacts/diagnosis_active.json /tmp/` (restore with the
reverse `mv`; the image ships the pointer + joblib, `docs/demo-script.md`
pre-flight step 3). Verified locally: moved
`backend/artifacts/diagnosis_active.json` aside, then **restored it
byte-identical**.

> Cached-diagnosis caveat: an incident that already has a diagnosis keeps
> showing it (`_ensure_diagnosis` returns the latest). To see the fallback
> you need a fresh incident (demo reset + re-trigger) or, in a scratch DB,
> delete the incident's `diagnoses` rows and re-open the incident page —
> the first view auto-classifies.

**Observed backend (verified, pasted — same incident, artifact absent):**

```text
auto-diagnosis on first view: no_fault | conf 0.45 | model diagnosis-heuristic@heuristic-1
explanation: "[heuristic] Predicted no_fault (confidence 0.45). Top-3: no_fault 0.45,
              gateway_degradation 0.08, route_latency 0.08. Rules fired: no rule fired;
              ambiguous signature -> no_fault (weak)."
investigation (force_refresh): report confidence 0.45, escalated: true
  escalation_reasons: ["confidence 0.45 below escalation threshold 0.5"]
  recommended next step: escalate_human
  uncertainties:
    - "Diagnosis came from the rule-based fallback (no trained model artifact); treat its confidence as advisory."
    - "Diagnosis confidence 0.45 is below the 0.85 auto-execute floor — any automation takes the human-approval lane."
    - "Detection fired but the diagnosis reports no actionable fault; treat the anomaly as noise unless a human disagrees."
```

Heuristic confidences are capped at 0.70 (`_CONF["strong"]` in
`app/services/diagnosis/heuristic.py`), and strategy confidence =
diagnosis × action-fit — so with no artifact, **nothing can ever reach the
0.85 auto-execute floor**: every automation lands in the approval lane, and
the reasoner headlines `escalate_human`. This is approval-lane-only by
construction, not by config.

**Observed UI (verified screenshot):** diagnosis card: amber **`HEURISTIC
FALLBACK`** badge (vs blue `ML classifier`), `diagnosis-heuristic@heuristic-1`,
45% confidence bar, `[heuristic]` explanation; investigation panel:
`HEURISTIC REASONER` + `ESCALATED` badges, "Escalation required — a human
must review before any action".

**Restore + proof (verified):** pointer restored → cleared the incident's
cached diagnosis → first view auto-classified
`method_outage | conf 0.978722 | diagnosis-random_forest@v20260828T013109Z-77a4ef3b`
— bit-identical to the pre-chaos baseline.

**Honesty note for the talk-track:** the heuristic is a *safe fallback*, not
a mini-model — here it read the same incident as `no_fault` (weak rules, no
rule fired) where the ML model reads `method_outage` 0.9787. Do not claim it
approximates the model; claim it degrades to human-in-the-loop.

**Talk-track:** "If the model file never shipped, the system doesn't invent
confidence — it says 'heuristic' in amber, caps itself under the auto lane,
and hands the decision to a human. Automation earns its lane; it never
inherits it."

---

## Findings (reported, not fixed — no product code changed)

- **F1 (medium — demo-relevant): the Postgres engine sets no
  `connect_timeout`.** `sa.create_engine(settings.DATABASE_URL, ...)` in
  `backend/app/db.py` passes no connect timeout, and psycopg v3's default is
  "retry the connection indefinitely" (verified: refused connect still
  retrying at 35s). Consequence: a lost database turns every DB-touching
  endpoint into a hang (behavior 3a), not the designed fast
  `database: down` degradation (behavior 3b, verified with
  `?connect_timeout=3`). Under uvicorn, sustained DB loss would also exhaust
  workers. Suggested follow-ups for the lead: set a small default connect
  timeout for the Postgres engine, or document `?connect_timeout=3` in the
  compose `DATABASE_URL`. For a **live DB-down beat today**, prefer showing
  `/readyz` behavior with the timeout in the URL, or narrate the client-side
  10s timeout panels as the visible symptom.
- **F2 (low): top-level health status does not aggregate component
  checks.** With the database down, `/readyz` and `/api/v1/system/health`
  still return HTTP 200 with top-level `"status":"ok"` (only
  `checks.database.status` is `"down"`; the console's header pill echoes the
  green top-level status next to the red database row). An external
  readiness gate reading only the top level would route traffic to a DB-less
  instance. Either derive top-level status from checks or document that
  consumers must inspect `checks`.
- **F3 (cosmetic): stale error panels clear on their own poll cadence.**
  After backend recovery (chaos 1), fast panels (15–20s) self-heal but the
  60s-cadence timeseries panel can show a stale `Backend unreachable` panel
  for up to a minute. The Retry button refetches instantly. Worth one
  narrated sentence if a panelist spots it.
- **F4 (docs note): bare `postgresql://` URLs crash at boot**
  (`ModuleNotFoundError: psycopg2`; the stack ships psycopg v3). Compose
  already uses `postgresql+psycopg://`; any hand-rolled `DATABASE_URL` must
  too.
- **No critical findings.** All nine failures landed in their designed safe
  states: fail-closed signature gate, zero-side-effect 400s, one-mutation
  discipline, GET-only resolution, bounded LLM fallback, capped heuristic
  confidence with human escalation, honest health (with the F1/F2 caveats).

## Evidence index (all produced 2026-08-28, this repo HEAD)

- Seed: `upi_outage_demo` → 41,354 rows, incident
  `inc_1e645c47bb3441d09316d10f62c8ec53`, 1 CRITICAL anomaly (−76.28%,
  ₹52,677 at risk) — matches `docs/demo-script.md` Appendix B.
- Baseline (pre-chaos): ML diagnosis `method_outage` 0.9787218468468468 on
  `diagnosis-random_forest@v20260828T013109Z-77a4ef3b`.
- Chaos 1: curl exit 7 + port-free check; screenshots of the down state
  (`Backend unreachable`, `API · OFFLINE`) and the self-healed state.
- Chaos 2: health `razorpay_test`; FAILED + `GatewayAuthenticationError`;
  stub log (1 POST); missing-keys variant health `simulator`.
- Chaos 3: hang timings (`/healthz` 0.031s vs `/readyz` >15s), psycopg
  >35s retry proof; `connect_timeout=3` `/readyz` + `/api/v1/system/health`
  payloads (3.01s/3.03s); screenshots of timeout panels and the red `down`
  pill.
- Chaos 4: two 400 payloads + request-id-matched WARNING log; row counts
  0/9405/21135 unchanged.
- Chaos 5: three webhook acks `processed:true`; action walk
  VERIFYING→FAILED→RECOVERED→(no-op); `payment_events`
  `failed→captured/webhook` row; transition logs with triggers.
- Chaos 6/7: execute UNKNOWN payload; stub access logs (1 POST + backed-off
  GETs only); resolve RECOVERED payload; audit rows
  `unknown/resolve_check/recovered`; `attempts: 1`; Approval-center
  screenshot (`Needs resolution · 1`, `Re-query gateway truth` button).
- Chaos 8: degraded investigation payload (reasoner/generated_by/
  degraded_reasons); stub log (2 LLM calls); screenshot of the red DEGRADED
  badge + reason line.
- Chaos 9: heuristic diagnosis + escalated investigation payloads;
  screenshot of the amber `HEURISTIC FALLBACK` badge; post-restore
  `method_outage` 0.978722 (bit-identical) proof.

## See also

- `docs/demo-script.md` — the 5-minute panel runbook; Appendix C condenses
  this file to a per-beat table.
- `backend/scripts/demo_live.py` — rehearsed live beats (`captured`,
  `beat-d`, `beat-e`) for the compose stack.
- `docs/demo.md` — the deterministic CLI proof suite.

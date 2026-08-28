# PulseRecover — Live Demo Script (hiring panel, 5 minutes)

The deterministic, rehearsed runbook for the live container demo. Every number
below was produced by real rehearsal runs against the deployed compose stack
(two full passes, 2026-08-28 — see Appendix B for the pass logs and the
determinism diff; re-verified the same day against the exp07 diagnosis
artifact `random_forest v20260828T013109Z-77a4ef3b`). Nothing is mocked and
nothing is scripted that the system did not actually do.

> Probabilistic AI proposes. Deterministic policy decides. Payment
> infrastructure executes. Verification proves.

**Narrative:** Problem → Detection → Diagnosis → Revenue at risk → Recovery
strategy → Policy gate → Razorpay action → Verification → Revenue recovered →
Failure handled safely.

**Stack (demo-day ports):**

```bash
BACKEND_PORT=8100 FRONTEND_PORT=3200 DB_PORT=55432 \
  docker compose -f deploy/docker-compose.yml up -d
# console: http://localhost:3200   api: http://localhost:8100
```

---

## 0. Pre-flight checklist (15 min before the panel)

- [ ] Docker daemon up (`docker info`).
- [ ] Ports free: 8100, 3200, 55432 (3000/8000 belong to other processes —
      never touch them; 8001/3100 are the e2e suite's).
- [ ] Images current: `BACKEND_PORT=8100 FRONTEND_PORT=3200 DB_PORT=55432
      docker compose -f deploy/docker-compose.yml build` — the backend image
      ships the committed diagnosis artifact
      (`backend/artifacts/diagnosis_active.json` + the active joblib, ~9.8 MB),
      so container diagnosis is the real ML model, not the heuristic fallback.
- [ ] Stack up; all three checks green:

  ```bash
  curl -s http://localhost:8100/healthz                       # {"status":"ok"}
  curl -s http://localhost:8100/api/v1/system/health
  # database ok (Postgres), policy_engine ok (detail = "1.0+sha256.5a6afe61d6db"
  # — the gate REALLY loaded the file), gateway detail = "simulator"
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3200/   # 200
  ```

- [ ] Clean slate: `curl -X POST http://localhost:8100/api/v1/demo/reset`
      (keeps evaluation runs + the append-only audit trail by design).
- [ ] Evaluation lab pre-seeded (one completed run, ~55s — too slow to run
      live in a 5-minute slot):

  ```bash
  curl -X POST http://localhost:8100/api/v1/evaluation/run \
    -H 'Content-Type: application/json' -H 'X-API-Key: dev-key' \
    -d '{"name":"demo-panel-baseline","evaluation_type":"full"}'
  ```

- [ ] Terminal open at repo root for the four `docker compose exec` beats;
      browser open on http://localhost:3200.

> **Numbers caveat:** the simulator anchors its dataset to *today* 00:00 UTC,
> so absolute figures are deterministic **within a calendar day**. The numbers
> below are the 2026-08-28 rehearsal; on demo morning re-run the pass once
> (§6) and confirm the talk-track figures — the structure (severity, gates,
> lanes, outcomes) never changes.

## 1. Minute-by-minute script

### 0:00–0:30 — Problem (Command Center, `/`)

- **Show:** Command Center. Point at the Demo control panel.
- **Say:** "PulseRecover watches payment health for a merchant. I'll inject a
  realistic prime-time UPI outage into this deterministic simulator — 10 days
  of traffic, about 41,000 rows — and the platform has to do the rest."
- **Do:** Demo control → **Run** on `upi_outage_demo`
  (or `curl -X POST http://localhost:8100/api/v1/demo/scenario/upi_outage_demo`).
- **Expect:** ~13–20s (UI client allows 120s; "Seeding… can take up to a
  minute" is normal). Response: 41,354 rows seeded; one anchored detection
  pass → **1 anomaly, 1 incident**.
- **Fallback:** if the UI button shows "No response within 10 seconds", keep
  talking — the run continues server-side and the dashboard poll (15s)
  surfaces it; the run is idempotent, never re-click frantically.

### 0:30–1:15 — Detection (Command Center → incident card)

- **Say:** "Detection anchored just after the outage window and fired:
  success rate fell **84.3% → 20.0% (−76.3%)** — severity **CRITICAL**,
  **₹52,677** estimated at risk, **80 failed payments** in the blast radius."
- **Expect (verified):** metric `payment_success_rate`, deviation −76.28%,
  severity CRITICAL, `revenue_at_risk_paise` 5,267,730.
- **Do:** click the incident.

### 1:15–2:00 — Diagnosis (Incident detail, `/incidents/{id}`)

- **Say:** "The ML root-cause model — a random forest trained on production
  detection frames, shipped *inside* the image — reads this as `method_outage`
  with **0.9787 confidence**. Not a heuristic: model version
  `v20260828T013109Z-77a4ef3b`, held-out top-1 0.910 on exact spans and 0.715
  on production 12-hour frames."
- **Expect (verified):** diagnosis card: `method_outage`, confidence 0.9787,
  model `diagnosis-random_forest@v20260828T013109Z-77a4ef3b`.
- **Then:** revenue panel — **Say:** "The counterfactual engine confirms
  **₹52,677** observed loss across the window and estimates **₹29,804** of
  it recoverable."
- **Do:** **Run investigation** button.
- **Say:** "The AI investigator explains the evidence and ranks hypotheses.
  It only *advises* — it can touch nothing."
- **Fallback:** if diagnosis shows `heuristic` in the model name, the artifact
  didn't ship in the image — rebuild (pre-flight step 3) and re-run; do NOT
  improvise a different story, the numbers won't match.

### 2:00–2:45 — Recovery strategy (Recovery planner, `/recovery`)

- **Do (UI):** on the incident page, click **Build recovery opportunities** →
  **Confirm build**. It is idempotent — a second run shows `0 created · 100
  already existed`, which is itself a demo-worthy beat. (Terminal equivalent
  if a panelist asks for the API:

  ```bash
  curl -X POST http://localhost:8100/api/v1/recovery/opportunities/build \
    -H 'Content-Type: application/json' -H 'X-API-Key: dev-key' \
    -d '{"incident_id":"<incident_id>","actor":"human:demo"}'
  ```

  )

- **Expect (verified):** **113 opportunities** created (103 failed-payment
  retries + 10 stuck-checkout recoveries), **₹73,071** of failed payments in
  scope.
- **Show:** the planner list; open the smallest retry (₹100 UPI timeout) —
  several opportunities sit at ₹100; open the `failed_payment_retry` whose
  plan shows `retry_payment` at confidence **0.9591** (expected ₹35.00).
- **Say:** "Each opportunity gets ranked strategies with expected recovery
  and confidence. This retry scores 0.9591 — and the gate *previews* the
  policy decision: ALLOWED."
- **Expect:** plan shows `retry_payment`, confidence 0.9591, expected
  recovery ₹35.00, policy preview **ALLOWED**.

### 2:45–3:30 — Policy gate + Razorpay action + verification (two lanes)

**Auto lane (₹100):**
- **Do:** Execute (or curl `POST /api/v1/recovery/{opp}/execute
  {"actor":"agent:strategist"}`).
- **Say:** "Under ₹5,000 with confidence ≥ 0.85, the deterministic gate
  allows autonomous execution. It fires exactly one gateway call — a fresh
  order with our idempotency key as the receipt."
- **Expect:** status **VERIFYING** ("executed; awaiting webhook
  verification") — real Razorpay answers `created` here too; capture is
  always proven by webhook.
- **Do (verification beat):**

  ```bash
  docker compose -f deploy/docker-compose.yml exec backend \
    python scripts/demo_live.py captured --opportunity-id <opp_id>
  ```

- **Say:** "The signed `payment.captured` webhook lands — HMAC verified,
  event id deduped — and the action closes **RECOVERED**. ₹100 back."
- **Expect:** HTTP 200 `processed:true`; action RECOVERED.

**Approval lane (₹5,656):**
- **Do:** Execute the largest opportunity.
- **Say:** "Same AI, bigger money: ₹5,656 is above the ₹5,000 auto ceiling —
  the gate says **REQUIRES_APPROVAL** (`approval.amount`) and parks it in
  PENDING_APPROVAL. The AI cannot talk its way past this."
- **Do:** Approval center → **Approve** (actor `human:ops`) — status
  APPROVED ("approved by human; ready to execute"). Then fire it: recovery
  planner → open the opportunity → strategy comparison → **Execute**
  (curl equivalent: `POST /api/v1/recovery/{opp}/execute
  {"actor":"human:ops"}` a second time). Approving does NOT fire the
  executor — only the explicit execute does, and it fires exactly once.
- **Say:** "A human approves; only then does the executor fire — once —
  and the same webhook proof closes it RECOVERED."
- **Do:** the `demo_live.py captured` beat again for this opportunity.
- **Expect (verified):** PENDING_APPROVAL → (approve) → APPROVED →
  (execute) → VERIFYING → (webhook) → **RECOVERED**.
- **Fallback:** if a webhook beat reports `duplicate:true`, the event id was
  already consumed (same payment re-verified) — the action is already
  RECOVERED; say "dedupe just proved itself" and move on.

### 3:30–4:15 — Failure handled safely (two beats, terminal + UI)

**Beat D — gateway timeout → UNKNOWN, no blind retry, truthful resolve:**

```bash
docker compose -f deploy/docker-compose.yml exec backend \
  python scripts/demo_live.py beat-d --incident-id <incident_id>
```

- **Say:** "The gateway 503s mid-execution. The action lands in **UNKNOWN** —
  executed but unverifiable. Automation pauses. The operator's re-execute
  does a GET-only re-query instead of a second mutation — the mutation
  counter stays at **1**. When the gateway recovers and its own records show
  the capture, the same re-query resolves it **RECOVERED on evidence, never
  on hope**."
- **Expect (verified):** `Rs 518` UPI soft-decline; UNKNOWN → re-query (1
  mutation total) → RECOVERED. Visible in the UI + audit trail afterwards.

**Beat E — unsafe AI recommendation → POLICY BLOCKED:**

```bash
docker compose -f deploy/docker-compose.yml exec backend \
  python scripts/demo_live.py beat-e --incident-id <incident_id>
```

- **Say:** "We plant a compromised AI output: refund ₹534, confidence 0.99.
  Confidence is not authority. The gate matches `allowlist` +
  `never_auto_execute.refund` — **BLOCKED**, action **REJECTED**, and the
  block happens *before* any gateway call: zero money moved. The block itself
  is audited."

### 4:15–4:40 — Audit trail + revenue recovered (`/audit`, Command Center)

- **Show:** the Audit trail — every transition with actor and policy
  version (`1.0+sha256.5a6afe61d6db`), append-only. Rows are filed per
  entity (`recovery_action`, `policy_decision`, `recovery_opportunity`):
  filter by an action or opportunity id, or scroll the stream — the
  incident id itself carries only the two revenue-at-risk refresh rows.
- **Show:** Command Center: **Recovered revenue ₹6,274** (100 + 5,656 + 518),
  revenue at risk now **₹46,921**, pending approvals 0.
- **Say:** "Every rupee claimed is backed by a signed webhook and an audit
  row."

### 4:40–5:00 — Evaluation lab (`/evaluation`)

- **Show:** the pre-seeded run `demo-panel-baseline-exp07` (the 2026-08-28
  re-run against the shipped RF artifact; on a freshly seeded volume the
  pre-flight run is named `demo-panel-baseline` — same battery, same code).
- **Say (verified numbers, 2026-08-28 re-run):** "We measure ourselves the
  same way: the current battery reads detection
  **precision 0.33, recall 0.67** (12 incidents, 4 matched of 6 ground
  truth); diagnosis **top-1 1.0, top-3 1.0** on those matched detection
  windows; **zero unsafe actions** across the run; and the
  randomized-holdout arm isolates *incremental* lift against organic
  recovery — today it reads **−1.0 pp with a 95% CI of [−4.6, +2.1]**,
  which brackets zero: at this policy envelope the fleet-level effect is
  too small for the run to resolve, and the lab shows the CI instead of a
  bare point. That's the discipline we'd bring to Razorpay's numbers."
- **Do NOT** trigger a fresh run live (~55s — over budget); offer to run it
  for questions after.

## 2. Reset procedure (between panels / rehearsals)

```bash
curl -X POST http://localhost:8100/api/v1/demo/reset
```

Clears every simulator row and all derived state (incidents, diagnoses,
opportunities, strategies, actions, policy decisions, webhook events).
Keeps — by design, and say so if asked: `evaluation_runs`, `experiments`,
`model_predictions`, and the append-only `audit_logs` (one reset row is
added). No leaked state between passes: two consecutive rehearsals produced
identical key numbers (Appendix B).

## 3. Fallback plan (per step)

| If… | Say / do… |
|---|---|
| Scenario trigger >10s in UI | Normal — it seeds 41k rows. The UI says "still running server-side"; wait for the 15s poll or curl the endpoint once. Runs are idempotent. |
| Diagnosis shows `heuristic` | Image lacks the artifact → rebuild (pre-flight 3). Until then: "the heuristic fallback is the fail-safe; the ML artifact ships in the production image." |
| `captured` beat: `duplicate:true` | Dedupe proof — action already RECOVERED. Say exactly that. |
| Execute → PENDING_APPROVAL on the small opp | You picked one with conf < 0.85 — pick the ₹100 UPI timeout (smallest auto-eligible). The gate is honest; narrate the boundary. |
| Beat D says "no fresh auto-lane opportunity" | The incident's small opportunities are spent — reset and re-run the scenario (30s), then build opportunities. |
| Any 5xx from the backend | Show `/api/v1/system/health` — honest component status; restart: `docker compose -f deploy/docker-compose.yml restart backend`. The CLI proof suite (`python scripts/demo_run.py --scenario all`) runs the same story in ~2 min on any laptop. |
| Frontend blank/error panel | The console has designed error/empty states — refresh; error panels have retry. Backend is the source of truth; curl the same endpoint to prove it. |
| Docker dies | Fall back to the fully local deterministic CLI demo: `cd backend && .venv/Scripts/python scripts/demo_run.py --scenario all --db scripts/.demo.db` (proof: `python -m pytest tests/demo`). |

## 4. Teardown

```bash
docker compose -f deploy/docker-compose.yml down   # add -v to also drop pgdata
```

---

## Appendix A — Razorpay test-mode keys (if the panel asks for real mode)

The shipped demo runs on the simulation twin (no keys, no network). Real
test-mode is a config flip, proven fail-safe on 2026-08-27: with
`SIMULATION_MODE=false` and dummy keys the adapter initialises, health
honestly reports `gateway.detail = "razorpay_test"`, and the first real call
ends as a typed, definitive `GatewayAuthenticationError` (action FAILED —
"nothing happened"), no crash, no 500.

Setup (details + source links: `docs/razorpay-integration.md` §2):

1. Razorpay Dashboard → toggle **Test Mode** → generate keys (`rzp_test_*`).
2. `.env` (never commit): `SIMULATION_MODE=false`, `RAZORPAY_KEY_ID`,
   `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`,
   `RAZORPAY_BASE_URL=https://api.razorpay.com/v1`.
3. Dashboard → Webhooks: public URL → `POST /webhooks/razorpay`, events
   `payment.captured` / `payment.failed` / `payment_link.paid`, set the
   webhook secret (TLS 1.2+; separate test/live configs).
4. Deterministic failures: test cards per `error_reason`
   (`4100280000080001` → insufficient_fund, `4100280000090000` →
   payment_timed_out), UPI handles `success@razorpay` / `failure@razorpay`.
5. Limits to respect: max 30 payment links per business, no UPI payment
   links, card tokens live 3 days.

Mode selection is by **key prefix** (`rzp_test_` vs `rzp_live_`), not URL.
With no keys configured the factory always falls back to the twin — the app
can never silently hit the network.

## Appendix B — Determinism evidence (2026-08-28, two full passes)

Pass 1 and pass 2, each: reset → scenario → detection → diagnosis →
investigate → build → auto lane + approval lane → webhooks → beats D + E →
dashboard. Identical in both passes:

| Figure | Value (both passes) |
|---|---|
| Seed | 41,354 rows; trigger ~20s (client budget 120s) |
| Detection | 1 anomaly, CRITICAL, 84.3% → 20.0% (−76.28%), ₹52,677 at risk |
| Incident | baseline 0.843105, observed 0.2, 80 affected, ₹52,677 counterfactual (₹29,804 recoverable) |
| Diagnosis | `method_outage`, confidence **0.9787218468468468** (bit-identical), model `diagnosis-random_forest@v20260828T013109Z-77a4ef3b` |
| Opportunities | 113 created (103 retry + 10 stuck-checkout), ₹73,071 in scope |
| Auto lane | ₹100 UPI timeout, conf 0.9591, expected ₹35.00, ALLOWED → VERIFYING → RECOVERED |
| Approval lane | ₹5,656, REQUIRES_APPROVAL (`approval.amount`) → approve → RECOVERED |
| Beat D | ₹518, UNKNOWN → re-query (1 mutation) → RECOVERED |
| Beat E | ₹534 refund conf 0.99 → BLOCKED (`allowlist`, `never_auto_execute.refund`) → REJECTED |
| Dashboard | recovered ₹6,274; at risk ₹46,921; pending approvals 0 |
| Audit | append-only by design (ids are uuid4) |

(The 2026-08-27 rehearsal of this runbook — same structure, previous LR
artifact — recorded: 82.7%→20.0%, diagnosis 0.9961 on
`v20260826T234303Z-c5434878`, 100 opportunities, ₹534 auto pick, beat D
₹553, beat E ₹554, recovered ₹6,743. Kept as the cross-day, cross-model
continuity note; the simulator anchors to today 00:00 UTC, so absolute
figures are deterministic within a calendar day.)

## Appendix C — Failure chaos (live breakage beats, panel-optional)

Nine verified ways to break this stack *on purpose* and let the failure make
the sale. Full induction commands, pasted evidence, and recovery detail:
**`docs/demo-chaos.md`**. All nine were re-verified on 2026-08-28 against
this HEAD; no product code was changed. Restore whatever you break; the demo
DB is disposable (reset + re-trigger rebuilds it deterministically).

| # | Break it (on the compose stack) | The panel sees | Say |
|---|---|---|---|
| 1 | `docker compose -f deploy/docker-compose.yml stop backend` (then `start backend`) | Red `Backend unreachable` panels + Retry, `API · Offline` pill; self-heals on poll after restart | "Designed failure states, not a white screen. The backend is stateless; truth is in the DB." |
| 2 | `SIMULATION_MODE=false` + dummy keys, execute an action | Action **FAILED** "gateway definitively rejected (nothing happened)"; health had `razorpay_test` all along | "Wrong keys = one typed `GatewayAuthenticationError`, zero crash. Missing keys = the factory silently refuses the network and health says `simulator`." |
| 3 | `docker compose -f deploy/docker-compose.yml stop db` | **Read F1 first:** with the stock URL the endpoints *hang* — UI shows 10s `timeout` panels, `API · Connecting`; `/healthz` stays 200 | "Liveness ≠ readiness." (For the red `database: down` pill, add `?connect_timeout=3` to `DATABASE_URL` — see F1/F2 in `docs/demo-chaos.md`.) |
| 4 | POST `/webhooks/razorpay` with `X-Razorpay-Signature: deadbeef…` | HTTP **400** `invalid_webhook_signature`; `webhook_events` count unchanged | "Signature before parsing. A forged capture touched nothing — here are the row counts." |
| 5 | Signed `payment.failed` → `payment.captured` → late duplicate `payment.failed` on one VERIFYING action | Pill walks VERIFYING → FAILED → **RECOVERED**; late `failed` is a no-op | "Delivery is unordered; truth isn't. A late success recovers, a late failure can't claw back." |
| 6 | `… exec backend python scripts/demo_live.py beat-d --incident-id <id>` (Beat D) | Action **UNKNOWN** — "no blind retry, resolution by re-query"; mutation counter stays 1 | "Maybe-it-charged is the scariest state in payments. One mutation, ever; then only reads." |
| 7 | `/recovery` → **Approval center** after beat D | Amber UNKNOWN + `NEEDS RESOLUTION` card + **Re-query gateway truth** button → RECOVERED on gateway evidence, audited | "It would rather admit 'I don't know' than be wrong about your revenue — and climbs out on evidence." |
| 8 | `LLM_PROVIDER=openai` + `OPENAI_BASE_URL` at a garbage endpoint; re-run investigation | Blue `LLM reasoner · …(fallback: heuristic)` + red **DEGRADED** badge with the reason; report still complete | "The AI is advisory and untrusted: garbage is schema-rejected, retried once, quarantined — the gate never saw it." |
| 9 | `… exec backend mv /srv/artifacts/diagnosis_active.json /tmp/` (**restore after**; needs a fresh/uncached incident) | Amber **HEURISTIC FALLBACK** badge, confidence ≤0.7, investigation escalates to a human | "No model file, no invented confidence: capped, badged, human-in-the-loop. Automation earns its lane." |

## See also

- `docs/demo-chaos.md` — the nine-beat failure-chaos runbook (Appendix C expanded, with pasted evidence and findings F1–F4).

- `docs/demo-rehearsal.md` — the final-wave timed rehearsal of this runbook
  (two full passes, per-beat timings, first-60-seconds check).
- `docs/demo.md` — the deterministic CLI proof suite (scenarios A–E) and its
  twice-run assertions; `backend/scripts/demo_run.py`.
- `backend/scripts/demo_live.py` — the live-stack beats used above
  (`captured`, `beat-d`, `beat-e`); run inside the backend container.
- `docs/razorpay-integration.md` — the real adapter, webhook intake, and
  test-mode research.

# PulseRecover — Live Demo Script (hiring panel, 5 minutes)

The deterministic, rehearsed runbook for the live container demo. Every number
below was produced by real rehearsal runs against the deployed compose stack
(two full passes, 2026-08-27 — see Appendix B for the pass logs and the
determinism diff). Nothing is mocked and nothing is scripted that the system
did not actually do.

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
      (`backend/artifacts/diagnosis_active.json` + the active joblib, ~10 KB),
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
> below are the 2026-08-27 rehearsal; on demo morning re-run the pass once
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
- **Expect:** ~13–15s (UI client allows 120s; "Seeding… can take up to a
  minute" is normal). Response: 41,348 rows seeded; one anchored detection
  pass → **1 anomaly, 1 incident**.
- **Fallback:** if the UI button shows "No response within 10 seconds", keep
  talking — the run continues server-side and the dashboard poll (15s)
  surfaces it; the run is idempotent, never re-click frantically.

### 0:30–1:15 — Detection (Command Center → incident card)

- **Say:** "Detection anchored just after the outage window and fired:
  success rate fell **82.7% → 20.0% (−75.8%)** — severity **CRITICAL**,
  **₹48,891** estimated at risk, **73 failed payments** in the blast radius."
- **Expect (verified):** metric `payment_success_rate`, deviation −75.83%,
  severity CRITICAL, `revenue_at_risk_paise` 4,889,100.
- **Do:** click the incident.

### 1:15–2:00 — Diagnosis (Incident detail, `/incidents/{id}`)

- **Say:** "The ML root-cause model — a calibrated logistic regression
  shipped *inside* the image — reads this as `method_outage` with **0.9961
  confidence**. Not a heuristic: model version
  `v20260826T234303Z-c5434878`, held-out top-1 0.878."
- **Expect (verified):** diagnosis card: `method_outage`, confidence 0.9961,
  model `diagnosis-logistic_regression@v20260826T234303Z-c5434878`.
- **Then:** revenue panel — **Say:** "The counterfactual engine refines the
  estimate to **₹51,931** at risk across the window."
- **Do:** **Run investigation** button.
- **Say:** "The AI investigator explains the evidence and ranks hypotheses.
  It only *advises* — it can touch nothing."
- **Fallback:** if diagnosis shows `heuristic` in the model name, the artifact
  didn't ship in the image — rebuild (pre-flight step 3) and re-run; do NOT
  improvise a different story, the numbers won't match.

### 2:00–2:45 — Recovery strategy (Recovery planner, `/recovery`)

- **Do (terminal — the one non-UI step):** turn the incident's failed
  payments into opportunities:

  ```bash
  curl -X POST http://localhost:8100/api/v1/recovery/opportunities/build \
    -H 'Content-Type: application/json' -H 'X-API-Key: dev-key' \
    -d '{"incident_id":"<incident_id>","actor":"human:demo"}'
  ```

- **Expect (verified):** **100 opportunities** created, **₹64,349** of failed
  payments in scope.
- **Show:** the planner list; open the smallest retry (₹534 UPI).
- **Say:** "Each opportunity gets ranked strategies with expected recovery
  and confidence. This retry scores 0.8965 — and the gate *previews* the
  policy decision: ALLOWED."
- **Expect:** plan shows `retry_payment`, confidence 0.8965, expected
  recovery ₹160.20, policy preview **ALLOWED**.

### 2:45–3:30 — Policy gate + Razorpay action + verification (two lanes)

**Auto lane (₹534):**
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
  event id deduped — and the action closes **RECOVERED**. ₹534 back."
- **Expect:** HTTP 200 `processed:true`; action RECOVERED.

**Approval lane (₹5,656):**
- **Do:** Execute the largest opportunity.
- **Say:** "Same AI, bigger money: ₹5,656 is above the ₹5,000 auto ceiling —
  the gate says **REQUIRES_APPROVAL** (`approval.amount`) and parks it in
  PENDING_APPROVAL. The AI cannot talk its way past this."
- **Do:** Approval center → **Approve** (actor `human:ops`).
- **Say:** "A human approves; only then does the executor fire — once —
  and the same webhook proof closes it RECOVERED."
- **Do:** the `demo_live.py captured` beat again for this opportunity.
- **Expect (verified):** PENDING_APPROVAL → (approve) → VERIFYING →
  (webhook) → **RECOVERED**.
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
- **Expect (verified):** `Rs 553` UPI soft-decline; UNKNOWN → re-query (1
  mutation total) → RECOVERED. Visible in the UI + audit trail afterwards.

**Beat E — unsafe AI recommendation → POLICY BLOCKED:**

```bash
docker compose -f deploy/docker-compose.yml exec backend \
  python scripts/demo_live.py beat-e --incident-id <incident_id>
```

- **Say:** "We plant a compromised AI output: refund ₹554, confidence 0.99.
  Confidence is not authority. The gate matches `allowlist` +
  `never_auto_execute.refund` — **BLOCKED**, action **REJECTED**, and the
  block happens *before* any gateway call: zero money moved. The block itself
  is audited."

### 4:15–4:40 — Audit trail + revenue recovered (`/audit`, Command Center)

- **Show:** Audit trail filtered to the incident — every transition with
  actor and policy version (`1.0+sha256.5a6afe61d6db`), append-only.
- **Show:** Command Center: **Recovered revenue ₹6,743** (534 + 5,656 + 553),
  revenue at risk now **₹45,741**, pending approvals 0.
- **Say:** "Every rupee claimed is backed by a signed webhook and an audit
  row."

### 4:40–5:00 — Evaluation lab (`/evaluation`)

- **Show:** the pre-seeded run `demo-panel-baseline`.
- **Say (verified numbers):** "We measure ourselves the same way: detection
  **precision 0.78, recall 1.0** on the standard battery; diagnosis top-1
  0.67 at this small scale; **zero unsafe actions** across every run; the
  randomized-holdout arm isolates *incremental* lift against organic
  recovery — and when the lift isn't there at small scale, the lab says so.
  That's the discipline we'd bring to Razorpay's numbers."
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
| Execute → PENDING_APPROVAL on the small opp | You picked one with conf < 0.85 — pick the ₹534 one (smallest ≥ ₹500). The gate is honest; narrate the boundary. |
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

## Appendix B — Determinism evidence (2026-08-27, two full passes)

Pass 1 and pass 2, each: reset → scenario → detection → diagnosis →
investigate → build → auto lane + approval lane → webhooks → beats D + E →
dashboard. Identical in both passes:

| Figure | Value (both passes) |
|---|---|
| Seed | 41,348 rows; trigger 13.1s / 15.1s (client budget 120s) |
| Detection | 1 anomaly, CRITICAL, 82.7% → 20.0% (−75.83%), ₹48,891 at risk |
| Incident | baseline 0.827381, observed 0.2, 73 affected, ₹51,931 counterfactual |
| Diagnosis | `method_outage`, confidence **0.9961315854932278** (bit-identical), model `v20260826T234303Z-c5434878` |
| Opportunities | 100 created, ₹64,349 in scope |
| Auto lane | ₹534, conf 0.8965, expected ₹160.20, ALLOWED → VERIFYING → RECOVERED |
| Approval lane | ₹5,656, REQUIRES_APPROVAL (`approval.amount`) → approve → RECOVERED |
| Beat D | ₹553, UNKNOWN → re-query (1 mutation) → RECOVERED |
| Beat E | ₹554 refund conf 0.99 → BLOCKED (`allowlist`, `never_auto_execute.refund`) → REJECTED |
| Dashboard | recovered ₹6,743; at risk ₹45,741; pending approvals 0 |
| Audit | 442 → 668 rows (append-only by design, +226/pass; ids are uuid4) |

## See also

- `docs/demo.md` — the deterministic CLI proof suite (scenarios A–E) and its
  twice-run assertions; `backend/scripts/demo_run.py`.
- `backend/scripts/demo_live.py` — the live-stack beats used above
  (`captured`, `beat-d`, `beat-e`); run inside the backend container.
- `docs/razorpay-integration.md` — the real adapter, webhook intake, and
  test-mode research.

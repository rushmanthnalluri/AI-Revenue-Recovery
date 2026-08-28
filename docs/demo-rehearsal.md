# PulseRecover — Demo Rehearsal Report (2026-08-28, final hardening wave)

Two complete, timed passes of the 5-minute panel runbook (`docs/demo-script.md`)
against the **deployed compose stack**, each starting from `POST
/api/v1/demo/reset`. Every number below is copied from the pass logs — nothing
is estimated. Verdict: **the demo is repeatable and comfortably timed; two
full verification passes take 67.6s and 63.4s of machine time, leaving ~4
minutes of the slot for narration.**

## 0. Setup under test

- Stack: `BACKEND_PORT=8100 FRONTEND_PORT=3200 DB_PORT=55432 docker compose -f
  deploy/docker-compose.yml up -d` (console :3200, API :8100, Postgres :55432).
- Images **rebuilt** for this rehearsal: the previous `deploy-backend`
  (07:11 IST) / `deploy-frontend` (03:29 IST) images predated commits
  `786ee19…799dfc7` (landed 08:26 IST — stuck-checkout loop, security fixes,
  build-opportunities UI, exp07 RF artifact). Fresh images built 17:22
  (backend, 905 MB) and 17:26 (frontend, 308 MB) from HEAD `313890f` plus the
  uncommitted invariants wave in the working tree.
- Pre-flight health (both passes): `database ok`, `policy_engine ok
  (1.0+sha256.5a6afe61d6db)`, `gateway ok (simulator)`, `llm_provider
  disabled`, frontend `/` 200.
- Evaluation lab was already seeded in the volume (`demo-panel-baseline-exp07`,
  completed) — reset keeps it by design, so no ~55 s live run was needed.
- Driver: a bash script issuing the runbook's exact calls (UI beats via their
  documented curl equivalents, the three terminal beats via `docker compose
  exec backend python scripts/demo_live.py …`). Wall time per beat measured
  with a millisecond clock; per-request server time measured by curl
  `time_total`. Windows Git Bash process spawns add ~1.5–2.5 s of harness
  overhead per beat — the table shows both numbers. Three other hardening
  agents were active on the machine during the passes.

## 1. Timings — two full passes

| # | Beat | Pass 1 wall | Pass 2 wall | Server time (p1 / p2) |
|---|---|---|---|---|
| 1 | `POST /demo/reset` | 2.1 s | 1.6 s | 0.56 s / 0.36 s |
| 2 | Healthy baseline (health + FE 200 + dashboard) | 3.5 s | 2.7 s | 0.02–0.07 s each |
| 3 | Scenario trigger `upi_outage_demo` | 16.9 s | 15.2 s | **14.01 s / 12.82 s** |
| 4 | Detection visible (incidents list) | 1.8 s | 1.7 s | 0.01 s / 0.01 s |
| 5 | Incident detail (diagnosis) | 3.6 s | 3.3 s | 0.64 s / 0.70 s |
| 6 | AI investigation | 2.4 s | 2.6 s | 0.28 s / 0.43 s |
| 7 | Build opportunities | 4.2 s | 3.3 s | 1.93 s / 1.26 s |
| 8 | Strategy comparison (list + plan) | 3.2 s | 3.0 s | 0.16 s / 0.13 s |
| 9 | Auto-lane execution (₹100 retry) | 1.6 s | 1.9 s | 0.08 s / 0.08 s |
| 10 | Approval lane (execute → approve → execute) | 3.7 s | 3.7 s | 0.10 s / 0.17 s total |
| 11 | Webhook verification (2× `captured` + 2 GETs) | 9.9 s | 8.4 s | webhooks HTTP 200 ×2 |
| 12 | Recovered revenue (post-webhook dashboard) | 2.3 s | 2.2 s | 0.49 s / 0.24 s |
| 13 | Failure beats D + E (2× container beats) | 8.2 s | 7.2 s | rc=0 both, both passes |
| 14 | Audit trail + final dashboard | 3.3 s | 3.9 s | ≤0.40 s per call |
| 15 | Evaluation lab (runs + detail) | 3.7 s | 4.6 s | 0.02 s / 0.03 s |
| | **Total (all 15 beats incl. verification reads)** | **67.6 s** | **63.4 s** | |

Cumulative t+ at beat end (pass 1 / pass 2): reset 2/1 · baseline 5/4 ·
scenario 22/19 · detection 23/20 · detail 27/24 · investigate 29/26 · build
33/29 · strategy 36/32 · auto 38/34 · approval 41/37 · webhooks 50/46 ·
recovered 52/48 · failures 60/55 · audit 64/59 · eval 67/63.

## 2. The 15 story beats — verified checklist

Identical in both passes unless noted. ✅ = verified with the value shown.

1. ✅ **Reset** — HTTP 200; cleared 17 tables (pass 1: 113 opportunities, 679
   strategies, 5 policy decisions, 4 actions, 1 incident, 9,405 payments,
   21,137 payment events, …); kept `evaluation_runs`, `experiments`,
   `model_predictions`, `audit_logs` (by design); one reset audit row appended.
2. ✅ **Healthy baseline** — components all ok; policy
   `1.0+sha256.5a6afe61d6db`; gateway `simulator`; dashboard zeroed
   (`open_incidents=0 at_risk=0 recovered=0`); frontend `/` 200 in ~0.02 s.
3. ✅ **Scenario trigger** — `status=completed`, **41,354 rows** (1,500
   customers, 9,169 orders, 9,405 payments, 21,135 events; simulator runtime
   13,928 / 12,756 ms); one anchored detection pass → **1 anomaly, 1
   incident**, CRITICAL, −76.28 %.
4. ✅ **Detection visible** — list `total=1`, `payment_success_rate`, severity
   CRITICAL, deviation −76.28 %.
5. ✅ **Incident detail (diagnosis)** — baseline **0.843105**, observed
   **0.2**, **80** affected payments, at-risk **₹52,677.30** (5,267,730
   paise); diagnosis **`method_outage`, confidence 0.9787218468468468**
   (bit-identical across passes), model
   **`diagnosis-random_forest@v20260828T013109Z-77a4ef3b`** (the shipped
   exp07 artifact — not the heuristic fallback); revenue panel observed loss
   **₹52,677.30**, recoverable **₹29,804**.
6. ✅ **AI investigation** — `status=completed`, 7 tools called
   (`get_incident`, `get_payment_stats`, `get_failure_distribution`,
   `get_revenue_at_risk`, `get_recovery_candidates`, `get_customer_history`,
   `propose_recovery_strategy`), confidence 0.95, `degraded=false`,
   `reasoner=heuristic` (deterministic reasoner — the container honestly
   reports `llm_provider=disabled`), 245 / 381 ms.
7. ✅ **Build opportunities** — **113 created** (103 `failed_payment_retry` +
   10 `stuck_checkout_payment`), **₹73,071** in scope, idempotent
   (`existing=0` on first build).
8. ✅ **Strategy comparison** — smallest auto-eligible retry (₹100 UPI
   timeout): `retry_payment` **confidence 0.9591**, expected recovery
   **₹35.00**, plan-level policy preview **ALLOWED**; largest ₹5,656.
9. ✅ **Auto-lane execution** — gate **ALLOWED** (`auto_execute.ok`) → status
   **VERIFYING** ("executed; awaiting webhook/fetch verification"), one
   gateway mutation.
10. ✅ **Approval lane** — ₹5,656 → gate **REQUIRES_APPROVAL**
    (`approval.amount`: "amount 565600 paise exceeds the auto-execute ceiling
    of 500000 paise") → **PENDING_APPROVAL** → approve (`human:ops`) →
    **APPROVED** → explicit second execute → **VERIFYING**. (See rough edge
    #1 — the re-execute step is now explicit in the runbook.)
11. ✅ **Webhook verification** — two signed `payment.captured` POSTs, both
    HTTP 200 `{"duplicate":false,"processed":true}`; both opportunities then
    read back **RECOVERED** with `attempts=1`. Event ids are deterministic
    per payment (`evt_sim_16ad86f93c54d2f7`, `evt_sim_ab5d27178298973d`) and
    recurred identically across passes — cross-pass dedupe is no issue
    because reset clears `webhook_events`.
12. ✅ **Recovered revenue** — post-webhook dashboard **₹5,756** (₹100 +
    ₹5,656), at-risk ₹46,921.30, pending approvals 0.
13. ✅ **Failure beats** — **D**: `Rs 518 upi soft_decline`; gateway outage →
    action **UNKNOWN**; operator re-execute = GET-only re-query (**mutations
    stay at 1**); outage clears → **RECOVERED on evidence** ("exactly 1
    mutating call was ever attempted"). **E**: planted refund **₹534,
    confidence 0.99** → gate **BLOCKED** (`allowlist`,
    `never_auto_execute.refund`) → action **REJECTED**, zero gateway calls.
14. ✅ **Audit trail + final dashboard** — final: **recovered ₹6,274.00**
    (627,400 paise = 100 + 5,656 + 518), **at-risk ₹46,921.30**, lost 0,
    pending approvals 0, open incidents 1. Audit: append-only total 2,366 →
    2,618 rows across the two passes; per-entity rows carry actor + policy
    version (`recovery.action.*`, `policy.action_blocked`,
    `verify_recovered`, `incident.revenue_at_risk_refreshed` — incident-id
    filter shows only the 2 refresh rows, see rough edge #4).
15. ✅ **Evaluation lab** — persisted run `demo-panel-baseline-exp07`
    (completed): detection **precision 0.3333, recall 0.6667** (12 incidents,
    4/6 matched, MTTD 230 min); diagnosis **top-1 1.0, top-3 1.0** (4
    scored); **unsafe actions 0** (false-action rate 0.06); incremental lift
    **−1.01 pp, 95 % CI [−4.63, +2.09]** (treatment 13.79 % vs holdout
    14.80 %) — matches the runbook's quoted talk track exactly.

**Determinism verdict:** every substantive figure is identical across the two
passes (rows, severity, deviations, paise amounts, confidences, statuses,
outcomes). Only ids (uuid4), timestamps, audit-row counts, and millisecond
runtimes differ — exactly as the runbook's Appendix B claims.

## 3. First-60-seconds assessment

Cold panel view (`GET /` → 200, 26 KB, ~0.02 s): the Command Center HTML is
server-rendered with the money panels visible in the markup itself ("Command
Center", "Demo control", "Recovered", "at risk"); data hydration is
client-side from `:8100` (measured API latencies 0.01–0.08 s). Subpages
(`/incidents/{id}`, `/recovery`, `/audit`, `/evaluation`) serve the app shell
(200, ≤0.27 s) and hydrate client-side — fine for an ops console.

Measured first-minute timeline (pass timings):

- **0:00–0:05** — problem framed over the live console (baseline dashboard:
  success rate 87.5 % vs 84.1 % baseline, zero incidents).
- **0:05–0:20** — presenter clicks **Run** on `upi_outage_demo`; the platform
  seeds 41,354 rows in **12.8–14.0 s** while the presenter narrates ("10 days
  of traffic, prime-time UPI outage"). UI copy honestly says seeding can take
  up to a minute; client budget 120 s.
- **0:20–0:35** — detection lands: **1 anomaly, CRITICAL**, success rate
  84.3 % → 20.0 % (−76.3 %). The incident card shows money at risk
  (₹53,729 initial estimate for the first ~10 s, then ₹52,677 after the
  counterfactual refresh — both real; see rough edge #4).
- **0:35–1:00** — presenter opens the incident (0.64–0.70 s): ML diagnosis
  `method_outage` at **0.9787** confidence with the model version on the
  card, plus the revenue panel (₹52,677 lost, ₹29,804 recoverable).

**Verdict: YES** — problem, money-at-risk, and AI-value are all on screen
inside 60 seconds with ~25 s of slack even when the 15 s dashboard poll lands
badly.

## 4. Presenter's minute-by-minute card (measured)

| Slot | Beat | Do / say (numbers verified in both passes) | Machine time |
|---|---|---|---|
| 0:00–0:30 | Problem + trigger | Command Center → Demo control → **Run** `upi_outage_demo`. "41,354 rows, 10 days, prime-time UPI outage — the platform does the rest." | 12.8–14.0 s seed |
| 0:30–1:15 | Detection | "Success rate 84.3 % → 20.0 % (−76.3 %), CRITICAL, ₹52,677 at risk, 80 failed payments." Click the incident. | reads ≤0.1 s |
| 1:15–2:00 | Diagnosis + AI | "Random forest *in the image*: `method_outage`, 0.9787, `v20260828T013109Z-77a4ef3b`. Counterfactual: ₹52,677 lost, ₹29,804 recoverable." **Run investigation** (0.3–0.4 s): "It advises; it can touch nothing." | ~1 s total |
| 2:00–2:45 | Strategy | **Build recovery opportunities** → Confirm (1.3–1.9 s): "113 opportunities, ₹73,071 in scope." Open the ₹100 retry: `retry_payment` 0.9591, expected ₹35.00, preview **ALLOWED**. | ~2 s |
| 2:45–3:30 | Two lanes + proof | ₹100: **Execute** → VERIFYING (0.08 s). ₹5,656: Execute → PENDING_APPROVAL (`approval.amount`) → **Approve** → **Execute** again → VERIFYING. Then the two `captured` beats (~3–4 s each): "HMAC-verified, deduped, RECOVERED — ₹100 and ₹5,656 back." | ~12 s |
| 3:30–4:15 | Failure beats | D (~4 s): "Gateway 503 → UNKNOWN → re-query, mutations stay at **1** → RECOVERED on evidence, never on hope." E (~3 s): "Refund ₹534 at 0.99 confidence → **BLOCKED** before any gateway call. Confidence is not authority." | ~7 s |
| 4:15–4:40 | Audit + revenue | Audit trail (append-only, policy `1.0+sha256.5a6afe61d6db` on every row); Command Center: **recovered ₹6,274**, at-risk ₹46,921, approvals 0. | reads ≤0.4 s |
| 4:40–5:00 | Evaluation lab | Pre-seeded `demo-panel-baseline-exp07`: "precision 0.33 / recall 0.67; diagnosis top-1 1.0; **zero unsafe actions**; lift −1.0 pp, CI [−4.6, +2.1] — we show the CI, not a bare point." | reads ≤0.1 s |

Total machine time on the presentation path: **~50–55 s**. The remaining ~4
minutes is narration — the slot has real slack, including for the 14 s seed.

## 5. Rough edges

**Fixed in the runbook** (small edits to `docs/demo-script.md`, this wave):

1. **Approval lane was missing the post-approve Execute.** Following the old
   text verbatim (execute → approve → webhook) leaves the action at APPROVED
   and the webhook unmatched: recovered revenue reads ₹618 instead of ₹6,274
   (proved by a first botched driver pass, then corrected). The runbook now
   spells out APPROVED → explicit second **Execute** → VERIFYING, with the
   curl equivalent.
2. **₹100 tie.** Four opportunities sit at ₹100 (1 stuck-checkout + 3
   retries). The runbook now says to open the `failed_payment_retry` whose
   plan shows `retry_payment` at 0.9591 / ₹35.00.
3. **Evaluation run name.** The run whose numbers §4:40 quotes is
   `demo-panel-baseline-exp07` on the current volume; the runbook now names
   it (and notes the fresh-volume equivalent).
4. **Audit "filtered to the incident".** The API files trail rows per entity
   (`recovery_action`, `policy_decision`, …); the incident id itself only
   carries the two `revenue_at_risk_refreshed` rows. The runbook now says to
   filter by an action/opportunity id or scroll the stream.

**Flagged (no runbook/product change; presenter awareness):**

5. **At-risk figure moves while you watch.** The incident card shows the
   *current* at-risk estimate: ₹53,729 (initial) → ₹52,677 (counterfactual
   refresh, ~10 s later) → ₹46,921 (after the three recoveries). All real —
   don't read the card's number in the first seconds; the talk-track figure
   (₹52,677) is stable from ~1:15 onward.
6. **The container's investigator is the deterministic heuristic reasoner**
   (`llm_provider=disabled`; report says `reasoner=heuristic`,
   `degraded=false`, 7 tools, 245–381 ms). The runbook's wording ("explains
   the evidence and ranks hypotheses; it only advises") stays accurate — just
   don't claim a live LLM call if a panelist asks.
7. **`captured` event ids are deterministic per payment**, so re-running the
   beat for the same payment within one pass correctly reports
   `duplicate:true` (the runbook's fallback already narrates this as a
   dedupe proof).

## 6. Evidence excerpts (verbatim from the pass logs)

Scenario (pass 2):
`status=completed … rows total 41354 … runtime_ms: 12756 … anomalies_detected: 1, incidents_created: ['inc_c09e48d1c38b4d3483516df486ba359f'], severity: CRITICAL, deviation_pct: -76.28`

Diagnosis (both passes):
`method_outage conf=0.9787218468468468 model=diagnosis-random_forest@v20260828T013109Z-77a4ef3b`

Webhook (pass 1, small opp):
`[VERIFY] POST /webhooks/razorpay payment.captured for pay_S42_3823e3861700000004315 (HMAC valid, event evt_sim_16ad86f93c54d2f7) -> HTTP 200 {"status":"received",…,"duplicate":false,"processed":true,…}`

Beat D (pass 2, abridged):
`[EXECUTE] gateway timed out on the mutating call -> action UNKNOWN …`
`same action act_7815f33d63824d099e35ef3396a42f17 re-queried instead of re-fired (gateway mutations attempted: 1 total; still UNKNOWN)`
`[RESOLVE] GET-only re-query (fetch_payment) proves the capture -> action RECOVERED`
`[RESULT] … exactly 1 mutating call was ever attempted (Rs 518 at stake, never double-charged)`

Beat E (pass 2):
`[ATTACK] … refund for Rs 534 … (confidence 0.99 - confidence is not authority)`
`[POLICY] gate: BLOCKED (rules: allowlist, never_auto_execute.refund) …`
`[RESULT] action REJECTED - blocked by the deterministic policy gate … zero money moved`

Final dashboard (identical in both passes):
`FINAL recovered=627400 at_risk=4692130 lost=0 pending_approvals=0 open_incidents=1`

## 7. Teardown

After the passes the stack was brought down with `docker compose -f
deploy/docker-compose.yml down` (volume kept — the seeded evaluation runs are
part of the demo pre-flight). No stray containers, processes, or scratch
databases left behind; the rehearsal driver and pass logs were scratch files
outside the repo, removed after the numbers above were transcribed.

## See also

- `docs/demo-script.md` — the runbook this rehearsal timed (Appendix B holds
  the earlier same-day determinism evidence; this file is the final-wave
  re-verification).
- `backend/scripts/demo_live.py` — the three terminal beats.
- `docs/demo.md` — the in-process CLI proof suite.

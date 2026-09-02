# 5-Minute Demo Plan — PulseRecover (post-Phase-A state)

Designed from the ACTUAL application, not aspirations. Prerequisites from the roadmap: A1 (webhook subscription incl. `payment_link.paid`), A2/A3 (event derivation + detection cadence), A4 (one live RECOVERED), E4 (keep-warm ping active). Screen: the live deployment, Real Merchant environment unless noted.

---

## 0:00–0:30 — Problem
- **Screen:** Razorpay Dashboard (test mode) side by side with PulseRecover Command Center.
- **Say:** "Merchants lose 5–15% of revenue to failed payments and abandoned checkouts. Razorpay shows them the failures — nothing wins the money back. PulseRecover does, inside strict bounds."
- **Show:** the failed payments in the Razorpay dashboard; the same rows in PulseRecover → Payments with `RAZORPAY TEST` provenance badges.
- **Data source:** REAL_RAZORPAY (live sync). **Fallback:** screenshot from rehearsal (paid links + sync run).

## 0:30–1:00 — Product
- **Screen:** Command Center (Real Merchant), populated (A2/A3 landed).
- **Say:** "Connected to this merchant's Razorpay test account. Everything you see is live — and everything is labeled: real merchant data here, synthetic research data one switch away." (Flip the environment switch once to show the Research Lab banner, flip back.)
- **Show:** revenue-at-risk hero, success-rate chart, verified-recovered counter (C2), System Health card: `database ok · gateway razorpay_test · worker ok`.
- **API:** `/api/v1/dashboard/summary`, `/api/v1/system/health`. **Fallback:** Research Lab seeded scenario for identical visuals.

## 1:00–2:00 — Detection
- **Screen:** Incidents list (one fresh incident from the injected failure burst — the wave-2 failed payments or a scripted burst rehearsed that morning).
- **Say:** "The detection engine reads the payment-event stream, compares against a rolling baseline with noise floors, and only fires on evidence — here's the failure wave it caught, with the window and the numbers."
- **Show:** incident row: metric, deviation, window, severity. Open it → detection evidence block.
- **API:** `/api/v1/incidents`, detection detail. **Fallback:** trigger a Research Lab scenario live (`POST /api/v1/demo/scenario/...`) — clearly labeled synthetic.

## 2:00–3:00 — AI diagnosis
- **Screen:** Incident detail → diagnosis + AI investigation report.
- **Say:** "The diagnosis model classifies the root cause — here: insufficient-funds wave — with calibrated confidence. The AI investigator then builds an evidence-grounded report using nine read-only tools. It cannot touch money: every action it proposes goes through a deterministic policy gate."
- **Show:** confidence bar, feature contributions, the ranked candidate actions (wave-1 feature), the guardrail note ("targets/amounts verified against tool data").
- **API:** `/api/v1/incidents/{id}/investigate`. **Fallback:** heuristic reasoner output is deterministic — rehearsal capture is identical.

## 3:00–3:45 — Policy + recovery
- **Screen:** Recovery pipeline → opportunity drawer → plan.
- **Say:** "The policy engine decides what may fire: confidence floors, amount caps, cooldowns, duplicate guards. This one needs a human — watch the approval." Click **Approve** → status transitions live (PENDING_APPROVAL → APPROVED → EXECUTING).
- **Show:** policy decision record (rules matched), the audit trail appending in real time, the two-step confirm.
- **API:** `/api/v1/recovery/{id}/plan|approve|execute`. **Fallback:** pre-staged opportunity in PENDING_APPROVAL if the live one misbehaves.

## 3:45–4:15 — Verification (the climax)
- **Screen:** Razorpay Dashboard → Payment Links (the link PulseRecover just created, visible in the merchant's own account) → pay it on camera with test card `4111…` → back in PulseRecover.
- **Say:** "The recovery is a real Razorpay payment link in the merchant's account. The customer pays — the webhook fires `payment_link.paid` — and PulseRecover marks it RECOVERED only after checking amount and currency to the paisa."
- **Show:** action card flipping to **RECOVERED** (webhook-driven, A1 landed); verification evidence (expected vs actual paise); the webhook event row.
- **API:** `/webhooks/razorpay`, action detail. **Fallback:** if the webhook is slow, the GET-based resolve (Reconcile button) closes it live — and that's a feature beat, not a failure ("two independent verification paths").

## 4:15–4:40 — Failure recovery (the honesty beat)
- **Screen:** Settings → sync run summary with the quarantined subscription skip; Audit Trail → **Verify integrity**.
- **Say:** "Here's what happens when things break. This account doesn't have Subscriptions enabled — Razorpay 401s that endpoint — so the sync skips it, records the reason, and completes the rest. And the audit trail is hash-chained; anyone can verify it." Click Verify → **CHAIN VALID**.
- **Also say (10s):** "When Razorpay times out mid-action, the system never blind-retries a mutation — it marks UNKNOWN and re-queries the truth. Duplicate-execute is proven by a race test."
- **API:** `/api/v1/audit/verify`, sync_runs. **Fallback:** none needed — deterministic.

## 4:40–5:00 — Measured result + architecture
- **Screen:** Evaluation Lab (the re-anchored run, C1/G2) → architecture one-pager (refreshed, H2).
- **Say:** "And the measurement is honest: a randomized holdout inside the simulator — recovery with the gated loop versus doing nothing, with confidence intervals. Here are the numbers, including where we're conservative." (If C1 lift is positive: lead with it. If not: lead with recovery-per-intervention + zero-unsafe + 98% fewer interventions — G2 framing.)
- **Close:** "971 tests, a public repo, deployed end-to-end on Razorpay Test Mode. Detection to verified recovery, with every action bounded and every rupee accounted for."
- **API:** `/api/v1/evaluation/metrics`. **Fallback:** metrics JSON screenshot from the stored run.

---

### Rehearsal checklist (run twice before recording)
- [ ] Keep-warm ping active; both services awake 10 min before recording.
- [ ] Fresh failure burst seeded ≤2h before (events → incident ready).
- [ ] One opportunity staged in PENDING_APPROVAL with clean amounts.
- [ ] Razorpay dashboard logged in; test card at hand; webhook delivery log open in a tab.
- [ ] Empty-state screenshots captured as fallbacks for every beat.
- [ ] Browser: hide bookmarks bar, 125% zoom, dark console.

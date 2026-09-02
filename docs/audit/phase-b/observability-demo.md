# Phase B — Observability + Demo-Compression Analysis

Analyst: Observability & Demo-Compression specialist (read-only). Date: 2026-09-02.
Scope: Part 1 = lineage/visibility improvements that make a judge trust the system faster (worker, health, payment_events/webhook_events/audit_logs/recovery_actions). Part 2 = critique + tightening of `docs/audit/demo-plan.md` against the live UI.
Evidence classes: **CODE** (path:line, read this session) · **LIVE** (curl against `https://pulserecover-api.onrender.com`, 2026-09-02 ~18:23 UTC) · **GATE** (phase-a-release-gate.md, not re-litigated).
No production files were modified; no tests were run; no git operations performed.

---

## Part 1 — Observability

### 1. Observations (verified facts the candidates build on)

1. **`webhook_events` is write-only from every consumer surface.** The only webhook endpoint is the POST intake (`backend/app/api/v1/webhooks.py:60`). `WebhookEventView` is defined but served by no router (`backend/app/schemas/webhooks.py:20`; grep over `backend/app` finds only the definition). The row carries everything a judge wants: `received_at` (indexed), `processed` (indexed), `processed_at`, `error`, `signature_valid`, `event_type`, unique `gateway_event_id`, `source` (`backend/app/models/system.py:120-136`). CODE.
2. **`/api/v1/system/health` has 5 checks — no webhook/ingestion check.** `database`, `policy_engine`, `llm_provider`, `gateway`, `worker` (`backend/app/api/v1/health.py:55-64`). LIVE payload confirms exactly these five: `{"status":"ok",...,"checks":{"database":...,"policy_engine":{"detail":"1.0+sha256.5a6afe61d6db"},...,"worker":{"status":"ok","detail":"last tick 17s ago"}}}` (curl 18:23:27 UTC).
3. **Webhook receipt IS tracked — one timestamp, buried in Settings.** `connection_state.last_webhook_at` is stamped on every verified delivery (`backend/app/services/recovery/webhook_handlers.py:131-145`), exposed via `GET /api/v1/merchant/connection` (`backend/app/api/v1/merchant.py:42-53`), rendered only at `frontend/src/components/settings/settings-view.tsx:277-281`. It proves receipt, not processing health. LIVE: `last_webhook_at: 2026-09-02T18:23:03Z` — 24 s before the fetch; webhooks are actively flowing.
4. **`payment_events` — the raw detection signal — has NO read API at all.** Payments API is list-only (`backend/app/api/v1/payments.py:26`); no events endpoint exists in any of the 15 routers (router-decorator grep, `backend/app/api/v1/*`). There is also no `GET /payments/{id}`. The append-only per-payment stream (`payment_id` FK, `event_type`, `from_status`→`to_status`, `source` = poller|webhook|simulator|seed|sync, `payload`, `occurred_at` indexed) sits unread (`backend/app/models/commerce.py:119-136`).
5. **Payment transitions produce no audit rows.** Grep for `entity_type="payment"` → zero writers; the webhook path audits `recovery_action` entities only (`backend/app/services/recovery/webhook_handlers.py:367-371,466-470`). The payment-side lineage record is `payment_events`; the action-side record is `audit_logs`. CODE.
6. **Every FK needed for full lineage already exists.** `PaymentEvent.payment_id` (`commerce.py:125-127`) · `RecoveryOpportunity.payment_id` + `.incident_id` (`backend/app/models/recovery.py:25-30`) · `RecoveryAction.incident_id` (denormalized), `.policy_decision_id` (real FK), `.gateway_request_id` (unique idempotency key), `.gateway_response`, full status timestamps (`recovery.py:101-130`) · `PolicyDecisionRecord.action_id` soft ref, `outcome`, `reasons`, `rules_matched`, `policy_version` (`recovery.py:140-155`) · `AuditLog` indexed on `(entity_type, entity_id)` (`backend/app/models/system.py:61-63`) with hash-chain fields (`:75-76`).
7. **Two partial lineage surfaces already exist — the chain's head is missing.** Incident detail composes a cross-entity timeline from audit + evidence + diagnoses and counts linked opportunities/actions (`backend/app/api/v1/incidents.py:185-260,276-291`). Opportunity detail embeds actions + latest policy decision + entity-filtered audit rows (`backend/app/api/v1/recovery.py:457-502`). Missing: the payment + event-stream start of the chain, and the payment→opportunity traversal (opportunity list filters are `status, incident_id, opportunity_type, customer_id` — no `payment_id`, `recovery.py:334-357`). In the UI the drawer shows `payment {id}` as plain mono text; only `incident_id` is a link (`frontend/src/components/recovery/opportunity-drawer.tsx:303-314`). The payments table has no row drill-in at all (`frontend/src/components/payments/payments-view.tsx:28-113`).
8. **Worker liveness is in-memory only; its durable footprints already exist but are hard to filter.** Supervisor holds `last_tick_at`, `last_error`, `tick_count` in process memory (`backend/app/services/worker/supervisor.py:47-49`); health reports tick age + stale degradation (`health.py:85-105`). `TickReport` is returned/logged, never persisted (`backend/app/services/worker/worker.py:94-108,186-196`). Durable worker evidence: `detection.run` audit rows with `trigger: "worker"` + anomaly/incident counts (`worker.py:74-91`), `notification.sent/retry_scheduled/failed` rows (`worker.py:311-327`), reconcile sweep rows. The audit list API filters by `entity_type, entity_id, environment` — **no `actor` filter** (`backend/app/api/v1/audit.py:26-39`), and the frontend dropdown hardcodes 7 entity types, omitting `detection_run`, `notification_outbox`, `connection_state` (`frontend/src/components/audit/audit-view.tsx:25-33`). `WORKER_DETECTION_SECONDS = 300.0` (`backend/app/config.py:61`) — the GATE-proven 300 s cadence.
9. **Sync history is trapped too.** `sync_runs` is append-only (`backend/app/models/sync.py:28-50`) but there is no GET list — only `POST /merchant/sync` returns a run (`merchant.py:64-92`). Settings renders the last run from component state, so a fresh page load shows nothing (`settings-view.tsx:154,341`).
10. **The health card renders checks generically — new checks need zero frontend work.** `SystemHealthCard` iterates `Object.entries(health.data?.checks ?? {})` (`frontend/src/components/command-center/system-health-card.tsx:24,74-91`). Any new key in the health payload appears on the Command Center automatically.
11. **Docs drift noted (not re-litigated):** `docs/audit/architecture-actual.md` §4.4/§7.6 says the worker never detects / runs three units; current `worker.py` runs four units including scheduled detection (`worker.py:1-24,177-185,353-368`), matching GATE evidence (worker-fired `detection.run` rows at 300 s cadence, phase-a-release-gate.md:12).

---

### 2. Candidate C1 — Webhook health on the health surface

**Classification: HIGH-VALUE · BUILD NOW** (the strongest candidate — see §5)

- **Observations:** #1, #2, #3, #10 above. The demo's climax is webhook-driven (demo-plan.md:37-41) yet no surface shows webhook liveness where a judge is looking.
- **Evidence:** all fields exist in `webhook_events` (`models/system.py:120-136`); `health.py` already has the DB session in hand (`health.py:52`); the aggregate-status machinery exists (`health.py:23-31`); UI renders new checks for free (`system-health-card.tsx:74-91`). LIVE baseline: last webhook 24 s before fetch (above).
- **Implementation concept:** add a `_webhook_check(db)` to `health.py` alongside `_worker_check`: one indexed query for `max(received_at)`, `max(processed_at)`, `count(*)` where `processed=false`, and latest `error` over a recent window. Output e.g. `webhooks: ok · "last received 12s ago · processed · 0 pending"`; `degraded` only on a nonzero unprocessed backlog older than the reconcile cadence or a recent handler error; `"none received yet"` reported as `ok` with honest detail (a fresh deploy is not a failure). Read-only GET; consistent with the open-GET posture (`architecture-actual.md` §2).
- **Dependencies:** none beyond existing tables/session. OpenAPI re-export if the contract file is refreshed (`backend/scripts/export_openapi.py` — response shape of `SystemHealth` is a map, so likely no contract change; UNCERTAIN until export is run).
- **Risks:** (a) a `degraded` webhook check flips top-level status to `degraded` — that is the *honest* design (`health.py:23-31`), but thresholds must not cry wolf during quiet periods → key on backlog age/errors, never on "no webhooks recently"; (b) the check adds one query per health poll — trivial on indexed columns; (c) simulator-source events (`source='simulator'`) must be excluded from the real-merchant signal or reported separately (`system.py:135`).
- **Test strategy:** unit tests mirroring the existing worker-check tests (stale/backlog/error/none-received matrix); the live probe pattern from Settings' webhook probe (`settings-view.tsx:170-176`) gives a manual end-to-end confirmation.
- **Demo value:** **highest per LOC in the system.** At the climax the judge watches `webhooks · ok · last received 8s ago · processed` flip on the Command Center health card seconds after paying the link — third-party-verified money movement made visible where they already are. It also fixes demo-gap D2 and operationally covers the product's #1 silent-failure mode (a dead webhook subscription).
- **Complexity:** **S** — one function + one dict entry in `health.py`, ~40 LOC + tests; zero frontend change.
- **Recommendation:** **BUILD NOW.**

---

### 3. Candidate C2 — One-payment lineage view (event → incident → action → verification)

**Classification: HIGH-VALUE · BUILD NOW (API first; UI drill-in can follow in the same wave)**

- **Observations:** #4, #5, #6, #7 above. Today no judge can answer "show me the full life of this one rupee" without four screens and manual id-copying.
- **Evidence:** every join key exists (obs. #6). What is missing is purely read composition: no `GET /payments/{id}` (`payments.py:26` list-only), no events endpoint (router grep), no `payment_id` filter on opportunities (`recovery.py:334-357`), no payment-entity audit rows (obs. #5 — `payment_events` is the payment-side record instead).
- **Implementation concept:** `GET /api/v1/payments/{payment_id}/lineage` (environment derived from the row's `source_type`, consistent with `payments.py:38`) returning one document: (1) payment row; (2) `payment_events` ordered by `occurred_at` (the event→incident edge is temporal + the opportunity's `incident_id`); (3) opportunities where `payment_id = X` with their actions; (4) the latest `policy_decisions` per action (pattern already written at `recovery.py:471`); (5) audit rows for the action/opportunity entity ids (pattern at `recovery.py:472-484`); (6) verification facts: `verified_at`, `gateway_request_id`, plus the `WebhookEvent.gateway_event_id` when the action's audit `details` reference it (UNCERTAIN — verify details payload shape before promising this field; if absent, omit rather than fabricate). Frontend: make the drawer's `payment {id}` text a link (`opportunity-drawer.tsx:305`) and/or add a row action on the payments table opening a lineage drawer reusing `Timeline` (`frontend/src/components/timeline.tsx`).
- **Dependencies:** existing models/queries only; no schema change. If added, the OpenAPI export + hand-written client (`frontend/src/lib/api.ts:224-405`) need one new method each (M9 in architecture-actual: client is hand-written).
- **Risks:** over-joining into a slow response — keep it to indexed single-entity queries (all FK columns indexed per models above); unauthenticated GET is consistent with current posture but the response aggregates more than any single existing GET — acceptable for demo-grade auth, note it. Environment isolation must reuse `source_types_for_environment` semantics, not a request param.
- **Test strategy:** service-level tests with seeded payment→event→opportunity→action→decision chains in both environments (isolation assertion: research payment id queried returns research chain only); API test for 404 on cross-environment id.
- **Demo value:** very high — this is the trust artifact. After RECOVERED, one screen shows: Razorpay event → detection incident → policy-gated action → webhook verification → hash-chained audit rows, every step timestamped. It converts the demo from "watch me click" to "audit it yourself".
- **Complexity:** **M** — one read endpoint (~120 LOC following existing patterns) + schema + optional drawer UI (~150 LOC).
- **Recommendation:** **BUILD NOW** (API + minimal drawer link; full lineage page is BUILD LATER if time-boxed).

---

### 4. Candidate C3 — Worker tick/activity surface

**Classification: C3a POSSIBLE (cheap) · BUILD NOW alongside C1 · C3b LOW-VALUE · REJECT for Phase B**

- **Observations:** #8 above. The GATE already proved 300 s detection via `detection.run` audit rows (phase-a-release-gate.md:12) — a judge currently cannot self-serve that proof.
- **Evidence:** durable worker footprints exist in `audit_logs` (`worker.py:74-91,311-327`); the audit API lacks an `actor` filter (`audit.py:26-39`); the UI dropdown omits worker-written entity types (`audit-view.tsx:25-33`); tick-level history is not persisted anywhere (`worker.py:94-108`, `supervisor.py:47-49`).
- **Implementation concept:** **C3a** — (i) add `actor: str | None = Query(default=None)` to `GET /api/v1/audit` (one filter line, `audit.py:29-39`); (ii) add `detection_run`, `notification_outbox`, `connection_state` to the frontend `ENTITY_TYPES` (`audit-view.tsx:25-33`); (iii) optionally include `tick_count` in the worker health detail (supervisor field already exists, `supervisor.py:49`; one format-string change at `health.py:98`). Result: a "worker activity feed" = `GET /api/v1/audit?actor=system:worker` showing detection passes every 300 s with anomaly counts. **C3b** — a persisted `worker_ticks` table (model + migration + writer) capturing every tick's TickReport.
- **Dependencies:** C3a: none. C3b: alembic migration (4 revisions at head `a83af82e8438`, baseline.md:24).
- **Risks:** C3a: none material (read-only, additive query param). C3b: write amplification (a row per 30 s tick forever), migration risk on Neon, and near-zero incremental judge value over C3a — the interesting units already audit themselves.
- **Test strategy:** C3a: audit-list API test with actor filter; C3b would need migration-fidelity tests (DEF-13 territory) — another reason to defer.
- **Demo value:** C3a medium-high (makes the cadence claim self-serve: filter to `detection.run`, show timestamps 5 min apart with `trigger: worker`); C3b low.
- **Complexity:** C3a **S** (~10 LOC backend + 3-line frontend); C3b **M+**.
- **Recommendation:** **BUILD NOW (C3a) · REJECT C3b for Phase B** (revisit if multi-process workers ever ship).

**Also considered, not shortlisted:**

| Idea | Classification | Verdict | Why |
|---|---|---|---|
| `GET /merchant/sync/runs` history list | POSSIBLE | BUILD LATER | Backs the honesty beat without clicking Sync live (obs. #9); but the live click is deterministic on this account (subscriptions 401 persists), so not blocking. |
| `tick_count` in health detail | POSSIBLE | BUILD NOW (folded into C3a) | One-line; shows the loop has ticked N times since boot. |
| Standalone `GET /payments/{id}` | EXISTING-gap | Subsumed by C2 | Useless without the events/actions around it. |
| Persist full TickReport history | LOW-VALUE | REJECT (C3b) | Cost > judge value; see above. |
| WebSocket/live-push lineage | SPECULATIVE | REJECT | Current 2.5–60 s react-query polling is proven (architecture-actual.md:10); a transport change for a demo is unjustified risk. |

Nothing reviewed was UNSAFE — all candidates are read-only compositions over existing tables.

---

## Part 2 — Demo compression (critique of `docs/audit/demo-plan.md`)

### Findings (severity-ordered)

- **D1 · HIGH — The climax is choreographed on a surface that does not update.** Plan 3:45–4:15: "action card flipping to RECOVERED (webhook-driven)" (demo-plan.md:40). The opportunity drawer's detail query is created **without** `refetchInterval` (`frontend/src/components/recovery/opportunity-drawer.tsx:264` calling the hook defined at `:35-41` — the interval parameter exists but is unused at the call site). The webhook arrives out-of-band; no invalidation fires; the judge stares at a stale VERIFYING card. Surfaces that DO poll: recovery list 10 s (`recovery-view.tsx:62`), pipeline panel 20 s (`pipeline-panel.tsx:168`), approvals 10–15 s (`approvals-panel.tsx:340-353`), Command Center summary 15 s (`command-center-screen.tsx:47`). Fix: pass `refetchInterval` (~5 s) into the drawer detail query — one line — or re-choreograph the flip onto the recovery list / Command Center "Recovered revenue" counter (`command-center-screen.tsx:209-216`).
- **D2 · HIGH — "Show the webhook event row" (demo-plan.md:40) is impossible: no such surface exists.** `webhook_events` has no GET API (Part 1 obs. #1) and no UI. The only webhook evidence on screen today: Settings' "Last webhook" timestamp (`settings-view.tsx:277-281`) and the action's audit rows in the drawer (`opportunity-drawer.tsx:236-257`). Fix: candidate C1 (health chip) + point at the drawer's audit trail for the RECOVERED transition row. Also "verification evidence (expected vs actual paise)" is not rendered as such anywhere — the amount/currency cross-check lives in handler code + audit details (`webhook_handlers.py:394-430`, `466-470`); the drawer shows `gateway_request_id` but not the paise comparison (`opportunity-drawer.tsx:212-216`). Either narrate from the audit row's `details` or drop the claim.
- **D3 · MEDIUM — Cold-open on a third-party dashboard.** 0:00–0:30 asks a judge who has never seen the product to parse Razorpay's dashboard AND PulseRecover side by side (demo-plan.md:8-11), and it spends the best screen (Command Center) as a side prop. Invert it: open on the Command Center alone; use the Razorpay dashboard exactly once, at 3:45, where its role is unambiguous ("the link is in the merchant's own account — I'm paying it now"). The failed-payments proof is better done on the Payments page: real rows with `Source` column provenance badges (`payments-view.tsx:99-102`) and gateway error codes (`:80-97`).
- **D4 · MEDIUM — The environment flip burns 10–15 s and invites confusion.** The flip at 0:30–1:00 (demo-plan.md:15) risks the judge losing track of which dataset they are watching for the rest of the demo. The provenance story is already ambient: a `ProvenanceChip` sits in every page header (`command-center-screen.tsx:111-115`, `payments-view.tsx:175`), and the switcher is part of the chrome with REAL MERCHANT / RESEARCH LAB labels (`sidebar.tsx:127-130`). Say it, point at the chip, don't flip. Flip only if a judge asks.
- **D5 · MEDIUM — The incident page carries two minutes and no scripted gaze path.** 1:00–3:00 lives on one dense page: stat band, metric chart, segment breakdown, decline-outlier insights, diagnosis card, investigation panel, audit timeline (imports at `incident-detail-view.tsx:18-40`). A cold judge will not know where to look; the presenter must name the three stops: deviation stat → diagnosis confidence → investigation report. CUT narration of segment breakdown and insights (leave them on screen). Note also the plan's "ranked candidate actions" in the diagnosis beat (demo-plan.md:28) actually live in the opportunity drawer's StrategyPanel (`opportunity-drawer.tsx:345-352`), not on the incident page — a presenter hunting for them live will stall.
- **D6 · LOW — The honesty beat forgets that the sync summary requires a live click.** Settings renders the sync-run summary only from session state after pressing "Sync now" (`settings-view.tsx:154,341`); the plan says "Screen: Settings → sync run summary with the quarantined subscription skip" (demo-plan.md:44) without saying to click. The quarantine itself is deterministic on this account (subscriptions 401 is persistent config — GATE rehearsal J), so the beat is safe *if scripted as*: click Sync now → watch the summary → quarantined rows.
- **D7 · LOW — The close crams two screens into 20 s and cites a stale test count.** 4:40–5:00 tries Evaluation Lab + architecture one-pager (demo-plan.md:50-52). Cut the architecture screen (a doc, not a product surface); keep ONE evaluation number (+0.59 pp, CI crosses zero, labeled inconclusive — the honesty differentiator). The scripted close says "971 tests" (demo-plan.md:52); current truth is 993 backend + 9 e2e (phase-a-release-gate.md:4).

### What already works — do not touch

- Command Center is the single best story-carrying screen: revenue hero, 6-KPI strip with provenance chip, 24 h chart with baseline, System Health card, recent incidents, recovery pipeline — all live-polled (`command-center-screen.tsx:39-347`).
- The policy beat is genuinely strong as written: the drawer shows the gate outcome badge, `policy_version`, rules matched, and reasons (`opportunity-drawer.tsx:169-210`), and approve/execute mutations invalidate their queries, so user-clicked transitions DO update live.
- Audit → Verify → CHAIN VALID is deterministic and GATE-proven (`audit-view.tsx:134`, phase-a-release-gate.md:48).

### Tightened beat sheet (5:00)

| Time | Screen (single focus) | Beat |
|---|---|---|
| 0:00–0:25 | **Command Center** | "Live, connected to a real Razorpay test merchant; everything labeled." Point: provenance chip, health card (`database ok · gateway razorpay_test · worker last tick Ns`). Do NOT flip environments. |
| 0:25–0:50 | **Payments** | The problem, over real data: failed rows, gateway error codes, RAZORPAY TEST badges. (Replaces the dual-dashboard open.) |
| 0:50–1:50 | **Incident detail** | Scripted gaze: deviation stat → diagnosis confidence → investigation report ("nine read-only tools; it cannot touch money"). No segment/insights narration. |
| 1:50–2:40 | **Recovery → opportunity drawer** | Strategy comparison → policy gate record (rules, version) → **Approve** → status flips (works today via invalidation). |
| 2:40–3:30 | **Razorpay dashboard → back** | First and only third-party screen: pay the recovery link on camera → return → webhook health chip shows "last received Ns ago · processed" (C1; else Settings timestamp) → RECOVERED flip on a polling surface (D1 fix) → the audit row in the drawer. |
| 3:30–4:10 | **Settings → Audit** | Click **Sync now** → quarantine summary (subscriptions 401, narrated as designed degradation) → Audit Trail → **Verify** → CHAIN VALID. |
| 4:10–4:40 | **Evaluation Lab** | One number: +0.59 pp, CI crosses zero, labeled inconclusive — "we publish the honest interval." |
| 4:40–5:00 | **Command Center** (return) | Close on the recovered counter having moved: "993 backend tests, 9 e2e, public repo, deployed on Razorpay Test Mode." CUT the architecture one-pager. |

Net changes vs. the current plan: −1 screen (Razorpay dashboard from the open), −1 screen (architecture doc), −1 gesture (env flip), +2 scripted clicks (Sync now, the gaze path), ±0 new product surface required — but D1/D2 each become non-issues if C1 + the drawer-poll one-liner land.

---

## 5. Summary recommendations

| # | Candidate | Class | Verdict | Complexity | Demo value |
|---|---|---|---|---|---|
| C1 | Webhook health check on `/api/v1/system/health` | HIGH-VALUE | **BUILD NOW** | S | Highest/LOC |
| C2 | `GET /api/v1/payments/{id}/lineage` + drawer drill-in | HIGH-VALUE | **BUILD NOW** (API first) | M | Very high |
| C3a | Audit `actor` filter + worker entity types in UI (+ `tick_count` in health detail) | POSSIBLE | **BUILD NOW** (with C1) | S | Medium-high |
| C3b | Persisted `worker_ticks` table | LOW-VALUE | **REJECT** (Phase B) | M+ | Low |
| — | `GET /merchant/sync/runs` history | POSSIBLE | **BUILD LATER** | S | Medium |
| — | WebSocket/live push | SPECULATIVE | **REJECT** | L | — |
| D1 | Drawer does not poll — climax choreo/fix | — | Fix (one line) or re-choreograph | S | Critical |
| D2 | "Webhook event row" surface does not exist | — | Resolved by C1 + drawer audit row | — | Critical |

**Single strongest BUILD NOW: C1 (webhook health on the health surface).** Every field it needs already exists in `webhook_events`/`connection_state`; it is ~40 LOC in `health.py` with zero frontend change (the health card renders checks generically); it repairs two HIGH-severity demo findings at once (D1's choreography fallback, D2's missing webhook surface); and it covers the product's top silent-failure mode — a dead webhook subscription — with the same honest-degradation semantics the worker check already uses. C2 is the close second and the better *artifact*, but it costs ~4× the effort and its UI half can wait a wave.

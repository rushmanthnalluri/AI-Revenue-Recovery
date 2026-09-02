# UI/UX Audit — PulseRecover Frontend

Captured: 2026-09-02. Auditor: UI/UX audit agent (audit Phases 6-7).
Target: https://pulserecover-web.onrender.com (Render free tier, cold starts) + 17 pre-existing screenshots in `docs/audit/screenshots/` (captured 2026-09-02 ~10:27-10:30 local by the earlier swarm attempt) + frontend source at `frontend/src/`.

Status vocabulary: WORKING / PARTIALLY_WORKING / BROKEN / MOCKED / SIMULATED / UNIMPLEMENTED / UNCERTAIN.
Severity vocabulary: CRITICAL / HIGH / MEDIUM / LOW.

**Live data context:** deployment has ~6 payments, 0 incidents, 0 recoveries (Razorpay Test Mode account, products limited). Empty states ARE the real first impression.

## Method

- [x] Read `docs/audit/baseline.md`
- [x] Inventory + analyze 17 existing screenshots (all 17 viewed; notes below per page)
- [x] Read frontend source (pages/components) to verify what screenshots show
- [x] Fresh Playwright probes for gaps (a11y attributes, DOM states, interactions) — 2 rounds, read-only clicks only (env toggle, tabs, Verify integrity; no Sync/Run/Approve/Execute/Reset/Download)
- [x] Per-page findings
- [x] 12 first-time-user questions
- [x] Severity-tagged findings list

New evidence files written by this pass (all in `docs/audit/screenshots/`): `live-home.png`, `live-recovery-approvals.png`, `live-audit-verify.png`, `live-evaluation-top.png`, `live-mobile-payments.png`, `live-mobile-nav-open.png`. Probe scripts kept outside the repo (`D:/tmp/ui-probe.cjs`, `D:/tmp/ui-probe2.cjs`), run with `NODE_PATH=frontend/node_modules`; no repo files modified except this document.

## Screenshot-analysis raw notes (evidence: files in `docs/audit/screenshots/`)

Global chrome (visible in every desktop screenshot, e.g. `home.png`):
- Left sidebar: logo "PulseRecover / REVENUE RECOVERY OPS"; ENVIRONMENT toggle (REAL MERCHANT | RESEARCH LAB); CONSOLE group (Command Center, Payments, Incidents, Recovery, Audit Trail); WORKSPACE group (Research Lab, Settings); bottom "API · LIVE" dot + "AI PROPOSES · POLICY DECIDES" tagline.
- Top bar: "PAYMENT RELIABILITY & REVENUE RECOVERY ENGINE" centered, "RAZORPAY TEST MODE · CONNECTED" green badge top-right.
- Every page header carries a provenance badge ("RAZORPAY TEST MODE · 1H WINDOW · 0 RECORDS" on home; "RAZORPAY TEST MODE · 6 RECORDS" on payments; "SYNTHETIC RESEARCH DATASET" on research-env home) plus "UPDATED JUST NOW · POLLS 15S".

- `home.png` (real merchant env): big empty-state panel "No payment activity yet — Process your first test payment …" + "Sync now" button; System Health card below (version 0.1.0, ENV prod, gateway razorpay_test ok, policy engine 1.0+sha256.5a6afe61d6db ok, llm provider none/disabled, worker last tick 2s ago ok). NOTE: badge says "1H WINDOW · 0 RECORDS" while payments page shows 6 records → empty state is window-scoped; copy "Process your first test payment" is factually wrong for an account that HAS payments (6, older than 1h). Candidate finding.
- `home-loading.png`: skeleton layout; KPI labels visible while loading (Recoverable revenue, Recovered revenue, Recovery rate, Active incidents, Payment success rate, Recoveries in flight); "API · CONNECTING" bottom-left; chart card "PAYMENT SUCCESS RATE — 24H / Hourly, anchored to the latest payment event".
- `payments.png`: 6 rows; columns PAYMENT (gateway id + internal id truncated), ORDER, AMOUNT (₹), METHOD (CARD/NETBANKING/WALLET), STATUS (failed red / captured green / created grey pills), ERROR REASON (BAD_REQUEST_ERROR + description truncated "Your payment didn't go through as it wa…"), SOURCE ("RAZORPAY TEST" badge per row). Filters: All statuses, All methods. "6 total · page 1 of 1", Previous/Next greyed.
- `incidents.png`: empty state "No incidents detected — When the detection engine flags a degradation in your observed Razorpay Test Mode activity, it will appear here." Filters: All statuses / All severities / All metrics. "0 total · page 1 of 1".
- `incident-detail-missing.png`: error card "Could not load this incident — incident not found: '00000000-…'" with CODE `incident_not_found:…`, REQUEST ID `52118f48af6b4aae8ec2a50dc50eeac8f`, Retry button, "← All incidents" back link. Good error state (request id surfaced).
- `recovery.png`: tabs Pipeline | Approval center. Pipeline: "RECOVERY PIPELINE — Every opportunity end to end…" empty state "No recovery opportunities — Opportunities are built from an open incident's failed payments and dropped checkouts — the build is idempotent and safe to re-run." + "Open the Command Center" link; "Run reconciliation" button. POLICY BACKTEST card: "Replay stored policy decisions against the current policy file — which verdicts would stand, which would flip, and the paise impact. Read-only report; only the run itself joins the audit trail." empty "No backtest run yet"; "Run policy backtest" button. (Approval center tab NOT captured by dead agent — gap to probe.)
- `audit.png`: copy "Append-only record of every state transition — actor, entity, and request id, newest first. Scoped by environment: research rows (scenario runs, dataset resets) never mix into the real merchant trail." 1 event visible: "Run detection.run", REAL TEST badge, actor `system:detection`, entity `detection_run · det_d09cbd757f254fc2bc674f0c51936f14`, req id shown, timestamp "2 Sept 2026, 10:06 am". Footer "1 events · page 1 of 1" → pluralization bug ("1 events"), LOW.
- `audit-verify.png`: after "Verify integrity": green banner "CHAIN VALID · 15 ROWS CHECKED · 15 CHAINED · 0 LEGACY". NOTE: UI shows 1 event in Real Test filter but verify checked 15 rows — verification scope (all envs?) vs display scope not explained; also header layout visibly squished ("EVENT STREAM / Chronological, immutable log rows — raw JSON details preserved" wrapped into narrow vertical strip next to filter bar) at 1483px width → layout bug candidate. Needs live re-probe to confirm.
- `research.png`: banner "RESEARCH SIMULATOR — synthetic data only; used for ML evaluation and controlled incident testing; not merchant data." Tabs Scenarios | Evaluation. Scenario runner copy "Deterministic simulator scenarios — seed the synthetic research dataset, run one anchored detection pass, and watch detection, diagnosis and recovery react. Research data only; the real merchant environment is never touched." 5 scenarios: payday_wave_demo ("14 days, month-end insufficient-funds wave"), quiet ("Clean baseline, no injected incidents"), standard ("30 days, ~65k events, one incident of each kind"), storm ("30 days, 8 incidents with overlaps (stress)"), upi_outage_demo ("10 days, prime-time UPI bank downtime"). Each with metric dropdown (PAYMENT_SUCCESS_RATE) + Run button. "Reset research data" button top-right. NOTE: two scenario names carry "_demo" suffix in prod UI (demo leakage candidate, LOW).
- `evaluation.png` (Research Lab > Evaluation tab): 1440x4247 full-page capture — very long page: scenario selector, "Run evaluation" button, dataset/card sections, metrics tables (13.7% / 16.3% style figures), detector comparison, "WHY THIS MATTERS" / glossary-type sections, per-incident table, "Research glossary" sections (Two arms same end, Should a payment…, The fairness (Equalized Odds)…, Detector score honesty, Models vs…, Synchronous execution…). Downsampled — needs region zoom or live re-probe for text QA.
- `settings.png`: RAZORPAY CONNECTION card: ENVIRONMENT "Razorpay Test Mode", CONNECTION "live Connected", KEY ID `rzp_test_••••sMjo` (masked), KEY SECRET "•••••••• NEVER LEAVES THE SERVER", WEBHOOK "ok Configured", AUTO SYNC Enabled, LAST SYNC "2 Sept 2026, 10:24 am COMPLETED", LAST WEBHOOK "2 Sept 2026, 10:13 am". Buttons: Sync now (primary), Test webhook, Disconnect. HOW TO CONNECT card (numbered setup steps, .env template block showing var NAMES only — `RAZORPAY_KEY_ID=rzp_test_…` placeholder form, no secret values). EXPORT DATA card: Export Type dropdown "Audit Trail — Append-only record of e…", Format "CSV — spreadsheet compatible", "Download CSV" button, "Environment: real_test". Good secret hygiene in UI.
- `not-found.png`: "404 — page not found. This console route does not exist. Head back to the Command Center." + Back to Command Center button; chrome intact.
- `home-research-env.png` (RESEARCH LAB env): top banner strip "SYNTHETIC RESEARCH — SIMULATOR DATA, NOT MERCHANT ACTIVITY" + top-right badge "SYNTHETIC RESEARCH". Full KPI deck renders here: Revenue at Risk ₹0 (RECOVERABLE ₹0 / RECOVERED ₹0 / CONFIRMED LOST ₹0), KPI cards: Recoverable revenue ₹0 "share of at-risk loss", Recovered revenue ₹0 "₹0 confirmed lost", Recovery rate 0.0% "recovered / all affected", Active incidents 0 "0 approvals pending", Payment success rate 100.0% "baseline 87.3% · 3 in 1h" with green "↗ +12.7 pp", Recoveries in flight 0 "executing or verifying". 24h chart renders axes + dashed baseline 87.3% but no visible success-rate line (says "current 100.0%"). Recent degradation empty ("Run a scenario from the Research Lab to watch the pipeline fire end-to-end."). Recovery pipeline card: RECOVERABLE ₹0 / IN FLIGHT 0 / AWAITING APPROVAL 0, "No opportunities yet".
- `mobile-home.png` (780px): hamburger + logo; header badges wrap awkwardly ("API · LIVE" wraps with dot on own line); content stacks correctly; empty state + System Health fine.
- `mobile-nav-open.png`: drawer nav covers ~85% width, X close top-right, backdrop dimmed, env toggle inside drawer. Functional.
- `mobile-payments.png`: table clipped — only PAYMENT + ORDER columns visible, ORDER truncated at viewport edge; AMOUNT/METHOD/STATUS/ERROR/SOURCE not visible without horizontal scroll (scrollability not provable from still — probe needed). MEDIUM candidate.
- `mobile-recovery.png`: stacks cleanly; filters full-width; empty states fine.

---

## Source-verified mechanics (evidence: path:line)

- **Command Center "empty" logic:** `frontend/src/components/command-center/command-center-screen.tsx:82-98` — `isFreshEnvironment` = `payments_observed === 0 && open_incidents === 0 && recovered === 0 && no recent incidents`. When true in real_test, ALL data chrome (RevenueHero, KPI MetricStrip, 24h chart, Recent degradation, Recovery pipeline) is replaced by one `EmptyState` "No payment activity yet — Process your first test payment…" + Sync now (lines 154-170); only SystemHealthCard stays (line 190).
- **`payments_observed` is event-stream-windowed, not all-time:** `backend/app/api/v1/dashboard.py:156-167` — anchor = latest terminal payment EVENT (`backend/app/services/detection/series.py:148-165`, `max(PaymentEvent.occurred_at)`), window = 1h before anchor; `payments_observed = n_current`. Docstring dashboard.py:11-13: "current success window: the hour ending at the latest terminal payment event".
- **PaymentEvents are webhook-born only:** the only `PaymentEvent(...)` constructor in backend is `backend/app/services/recovery/webhook_handlers.py:299`. REST sync (`Sync now`) writes Payment rows but no events → Command Center stays "0 RECORDS / No payment activity yet" even with 6 payments visible on /payments. Confirmed live: `home.png` ("1H WINDOW · 0 RECORDS") vs `payments.png` ("6 RECORDS"), same minute. ⇒ cross-page contradiction is structural, not a one-off.
- **Environment toggle:** `frontend/src/components/environment-provider.tsx:19-43` — real_test (default) | research, persisted to localStorage key `pulserecover:environment`; every scoped query threads it (e.g. command-center-screen.tsx:44-48, payments-view.tsx:131-142).
- **Label discipline rule vs reality:** `frontend/src/lib/environment.ts:12-14` — "the bare words 'simulation'/'demo' never appear user-visible". VIOLATED by scenario ids rendered verbatim in Research Lab: `payday_wave_demo`, `upi_outage_demo` (`research.png`; scenario names come from backend simulator registry). Only inside Research Lab, but the rule as written is broken. LOW.
- **Audit Verify scope:** `frontend/src/components/audit/audit-verify-action.tsx:14-20` — chain spans BOTH environments by design ("scoping would break linkage"); the only user-facing hint is the button `title` tooltip (line 36). The result strip ("15 ROWS CHECKED · 15 CHAINED · 0 LEGACY" in `audit-verify.png`) does not say it covers more rows than the 1 visible in the env-filtered stream. Tooltip invisible to touch/keyboard users. LOW-MEDIUM.
- **Audit trail count:** `audit-view.tsx:160` renders `${total} events` unpluralized → "1 events" (`audit.png`). Trivial LOW.
- **Payments table:** `payments-view.tsx:28-113` — 8 columns defined incl. CREATED; probe F2 measured the rendered table at 1160px wide vs ~1136px content box @1440px → CREATED clipped behind horizontal scroll at desktop too (only 7 columns visible in `payments.png`). Truncated cells have `title` tooltips (external_id line 34, order_id line 49, error_description line 89). Filters have `aria-label` (lines 184, 201); pagination count has `aria-live="polite"` (line 255). Rows NOT clickable (no onRowClick) — no payment drill-down exists.
- **DataTable a11y:** `data-table.tsx:88-103` — rows become keyboard-focusable (`tabIndex=0`, `role`, Enter/Space) only when clickable.
- **Approvals (financial safety):** `frontend/src/components/recovery/approvals-panel.tsx` — Reject/Escalate disabled until a reason note is typed (lines 197, 206 + explaining title tooltips lines 198, 207); policy gate verdict + version + rules + reasons shown per item (PolicySummary lines 37-85); UNKNOWN lane copy "Re-querying fetches the true payment state from the gateway (read-only GETs) — the mutation is never re-fired blindly" (lines 285-294); mutation errors via `role="alert"` (lines 169, 215, 308). Live queue empty → only the "Approval queue is clear" empty state (lines 402-410) is verifiable live.
- **Skip link + landmark:** `app-shell.tsx:26-31` skip-to-content (sr-only until focus), `main#main-content` (lines 35-38). Single 20s health poll owner (lines 15-22).

## Fresh live probes (Playwright chromium vs https://pulserecover-web.onrender.com, 2026-09-02)

### Probe round 1 (desktop 1440x900 + mobile 390x844)

**A. Home document + a11y chrome (live DOM):**
- `lang="en"`, `document.title = "Command Center"`, exactly one `h1`, **zero h2/h3 on the page** (card titles are not heading elements — flat heading outline for screen readers).
- Skip link present; `main#main-content` landmark; 1 `nav`; active nav link has `aria-current="page"`; **0 unnamed buttons; 0 imgs without alt; 0 unlabeled selects; 2 aria-live regions**.
- Contrast (computed styles, WCAG ratio): h1 17.02:1; body text 15.76:1; muted `.text-text-3` 5.58:1 (9.5px mono micro-labels — passes AA ≥4.5 but tiny); `.text-text-2` 7.43:1. (Two 1.00 readings for badge text were a probe artifact — effective-bg walk stopped at translucent badge backgrounds; corrected math in round 2.)
- Keyboard: first 14 Tab stops ALL show a visible 2px amber focus outline; order = skip link → env switcher → 7 nav links → "View all →" → "Open →" → body. Logical, no traps.

**B. Recovery → Approval center tab (live, screenshot `live-recovery-approvals.png`):** WORKING. Metric strip (Awaiting decision 0 · Value awaiting decision ₹0 · Needs resolution 0), then "AWAITING DECISION — The deterministic policy gate refused to auto-execute these — a human decision is required before anything fires." + empty state "Approval queue is clear — Nothing is waiting on a human decision. Items land here when the policy gate returns REQUIRES_APPROVAL…".

**C. Audit → Verify integrity:** round-1 click produced NO chain banner within 20s (timeout) — retried with 90s in round 2.

**D. Evaluation tab (live text, 8098 chars, 13 provenance hits):** a completed stored run exists (`console-2026-09-02-04-18`, scenario `standard`, end_to_end, 8.7m). UI labels: "Stored run row — the console never recomputes metrics". **The displayed results are honest NEGATIVE efficacy results:** Baseline vs PulseRecover — recovered revenue ₹9,90,116 vs ₹16,744 (−₹9.7L); recovery rate 27.0% vs 0.5% (−26.5 PP); interventions 4,893 vs 100 (98.0% fewer); false interventions 433 vs 10; ungated/unsafe actions 4,893 vs 0 → "SAFETY INVARIANT HELD". Holdout experiment: LIFT (ITT) −2.6 PP, 95% CI [−6.4, +0.7]; treatment 13.7% vs holdout 16.3% (treatment underperforms no-action); methodology section discloses priors, censoring, CI method, batch artifacts. Per-stratum tables with CIs. Detection: MTTD 7.0h, 5/6 ground-truth incidents matched; Diagnosis shows "MTTR 0S" (batch artifact, disclosed in methodology copy). ⇒ UI provenance/methodology labeling is exemplary; the UI faithfully shows the product UNDERPERFORMING both naive baseline and no-action control on the stored run (decision-relevant for synthesis — not a UI bug).

**E. Env toggle → Research Lab (live):** 4 simultaneous provenance signals — top strip "Synthetic research — simulator data, not merchant activity" (`role="note"`, topbar.tsx:42-49), top-right "Synthetic Research" badge, page description change, "Synthetic Research Dataset" chip. WORKING.

**F. Mobile round 1:** /payments on 390x844 never rendered `text=PulseRecover` in 3×60s — retried with diagnostics in round 2 (turned out to be cold-start flake).

### Probe round 2 (2026-09-02, same target)

- **C2 Audit Verify:** resolved in 1.86s (API warm; round-1 20s timeout = cold-start latency, not a bug). Banner "chain valid · 22 ROWS CHECKED · 22 CHAINED · 0 LEGACY" (row count grew 15→22 between probes — the worker writes audit rows continuously). **Layout bug CONFIRMED live at 1440px:** after the verify strip renders, the card header reflows so the "EVENT STREAM" title box = 82×34px and the description box = 82×80px — "Chronological, immutable log rows — raw JSON details preserved" wraps over ~8 lines in an 82px-wide column (screenshot `live-audit-verify.png`; identical squish in earlier `audit-verify.png` @1483px). Root cause: SectionCard header is `sm:flex-row` with the actions group `shrink-0` (`section-card.tsx:32-38`); 3 filters + verify button + the `basis-full` result strip leave 82px for the title block.
- **Titles:** `/payments`→"Payments · PulseRecover", likewise `/incidents`, `/recovery`, `/research`, `/settings`; unknown route keeps "PulseRecover — Command Center" (404 doesn't set its own title — trivial).
- **Contrast (corrected, fg on page bg rgb(11,13,12)):** success green 8.83:1, amber accent 8.80:1, danger red 4.73:1 (marginal AA pass), info slate 5.66:1. No contrast failures; round-1 1.00 readings were probe artifacts (translucent badge bg treated as opaque).
- **F2 Mobile 390×844 (HTTP 200, rendered fine):** payments table = **1160px wide inside an `overflow-x:auto` wrapper of 322px** — 3.6× viewport horizontal scroll; only PAYMENT + truncated ORDER visible without scrolling (screenshot `live-mobile-payments.png`). Same table at 1440px desktop: 1160px table vs ~1136px content box → the 8th column (CREATED, `payments-view.tsx:103-112`) is clipped behind horizontal scroll with no scroll affordance — consistent with `payments.png` showing only 7 columns.
- **F3 Mobile nav:** hamburger `aria-label="Open navigation"` + `aria-expanded`; drawer `role="dialog" aria-modal="true" aria-label="Primary navigation"` (screenshot `live-mobile-nav-open.png`). Correct semantics.

## Page: Home / Dashboard (`/`)

- **Status: PARTIALLY_WORKING** (chrome and health work; the data surface is structurally empty in real_test).
- **IA / hierarchy:** PageHeader (h1 "Command Center" + provenance chip + "updated just now · polls 15s") → in the current live state a single dashed empty-state panel → System Health card. The intended layout (RevenueHero → 6-KPI MetricStrip → 24h chart + health → recent incidents + recovery pipeline) only renders when the env is non-fresh (`command-center-screen.tsx:192-343`); it was visible in `home-research-env.png` (research env had seeded data earlier) and in the `home-loading.png` skeleton.
- **The first impression problem (HIGH):** live home says "No payment activity yet — Process your first test payment" while the account HAS 6 payments (visible one click away on /payments with real ₹ amounts). Root cause chain verified in code: dashboard aggregates read the webhook-born `payment_events` stream (`webhook_handlers.py:299` is the only constructor), windowed to 1h before the latest terminal event (`dashboard.py:156-167`); REST sync writes Payment rows only. So `payments_observed=0` → `isFreshEnvironment` → all product chrome (revenue-at-risk hero, KPIs, chart) is replaced by the empty panel (`command-center-screen.tsx:82-98, 131-191`). The copy claims "first payment ever"; the truth is "no terminal payment events in the analytics stream". A first-time user cannot reconcile the two pages, and nothing in the empty state explains the 1h/event-window semantics (the chip's "1H WINDOW · 0 RECORDS" is the only hint, and it contradicts "6 RECORDS" on /payments).
- **Loading:** full skeleton layout incl. KPI labels (`home-loading.png`) — labels visible before values, good. **Error:** ErrorPanel "Dashboard summary unavailable" + Retry (`command-center-screen.tsx:125-130`). Sync failure gets its own ErrorPanel (line 185-187).
- **States coverage:** not-connected, connected-no-data, unknown-connection-no-data, and research-empty each have distinct honest empty states with different CTAs (lines 133-183) — the state matrix is thoughtful; the copy of the connected-no-data branch is the one that's wrong for the actual deployment.
- **System Health:** version 0.1.0, ENV prod, gateway razorpay_test, policy engine `1.0+sha256.5a6afe61d6db`, llm provider "none · disabled" (honest — no fake AI), worker tick age, all with ok pills; polls 20s. Positive: "llm provider none/disabled" is truthful provenance.
- **Research env variant:** full KPI deck renders with zeros + a success-rate chart showing only the dashed baseline (no data line) — chart-without-data is slightly confusing but axes/baseline are labeled ("baseline 87.3%").

## Page: Payments (`/payments`)

- **Status: WORKING** (6 live rows render; filters/pagination present).
- **IA:** h1 + "RAZORPAY TEST MODE · 6 RECORDS" chip → one SectionCard "Observed payments" with status/method filters → 8-column table → "6 total · page 1 of 1" + Previous/Next (correctly disabled).
- **Provenance: exemplary** — per-row SOURCE badge ("RAZORPAY TEST" with `title="source_type: razorpay_test"`), provenance is "a first-class column, not a tooltip" (`payments-view.tsx:115-119`).
- **Readability issues:** (1) gateway id + internal id both truncated with the full value only in `title` tooltips (hover-only → inaccessible to keyboard/touch users); (2) error descriptions truncated ("Your payment didn't go through as it wa…") — again tooltip-only; (3) rows are NOT clickable — no payment detail view exists, so the row is the end of the road; (4) CREATED column clipped at ≤1440px (see probe F2). Amount right-aligned tabular — good. Status pills: failed red / captured green / created grey — with text labels (not color-only) — good.
- **Empty state (not currently visible):** distinct copy for no-data vs no-filter-match, plus a CTA link to Settings/Research (payments-view.tsx:149-164, 244-253) — well designed.

## Page: Incidents (`/incidents`)

- **Status: WORKING (empty state); populated state UNCERTAIN** (0 incidents live; never rendered with rows during audit).
- Empty state is honest and correctly attributes absence to the detection engine ("When the detection engine flags a degradation in your observed Razorpay Test Mode activity, it will appear here."). Three filters (status/severity/metric) render above an empty list — harmless. "0 total · page 1 of 1".
- Note: given the event-stream situation (HIGH finding on Home), this page will stay empty for real_test until webhook terminal events flow — a first-time user sees the product's core concept ("incidents") only as a promise.

## Page: Incident Detail (missing id) (`/incidents/<missing>`)

- **Status: WORKING (error state).** "Could not load this incident — incident not found: '00000000-…'" with machine CODE, a REQUEST ID for support correlation, Retry button, and "← All incidents" back link (`incident-detail-missing.png`). This is the model error-state pattern for the app.
- Populated detail (UNVERIFIED live — no incidents): source shows observed-vs-baseline stats, deviation tone logic, affected payments, revenue-at-risk with CI hint + "low confidence" badge (`incident-detail-view.tsx:97-107`), metric chart, segment breakdown, insights, diagnosis card, audit timeline, investigation panel, build-opportunities action with two-step confirm + focus management (`build-opportunities-action.tsx:16-78`).

## Page: Recovery (`/recovery`)

- **Status: WORKING (empty states); execution flows UNCERTAIN** (read-only rule — no Approve/Reject/Execute clicked).
- **IA:** h1 + env chip → tabs "Pipeline | Approval center" (proper ARIA tab pattern per research-view.tsx:84-114 implementation) → Pipeline tab: recovery pipeline card (filters + "Run reconciliation") + policy backtest card (env/window filters + "Run policy backtest"). Approval center: 3-metric strip + awaiting-decision queue + (conditionally) UNKNOWN lane.
- **Copy quality: high.** "Status is projected from the latest recovery action — webhook reconciliation updates actions directly"; "the build is idempotent and safe to re-run"; backtest: "Read-only report; only the run itself joins the audit trail"; approvals: "The deterministic policy gate refused to auto-execute these — a human decision is required before anything fires." These are the exact financial-safety assurances a payments operator needs.
- **Financial-safety affordances (source-verified):** reconciliation + backtest behind two-step confirms ("Confirm sweep"/"Confirm backtest", `reconcile-action.tsx:20-54`, `policy-backtest-panel.tsx:221-316`); Reject/Escalate require a typed reason (disabled + tooltip otherwise); UNKNOWN resolution is re-query-only ("the mutation is never re-fired blindly"); policy verdict/version/rules/reasons rendered per item; mutation errors via `role="alert"`; execution confirm uses `role="alertdialog"` (`strategy-panel.tsx:93-105`).
- Empty-state CTA "Open the Command Center" on the pipeline card is reasonable (incidents land there).

## Page: Audit (`/audit`)

- **Status: WORKING.** 1 event live (detection.run, REAL TEST badge, actor `system:detection`, entity chip, request id, timestamp, expandable DETAILS).
- **Copy: excellent** — "Append-only record… Scoped by environment: research rows (scenario runs, dataset resets) never mix into the real merchant trail."
- Env filter ("Real Test (Razorpay)" / "Research (synthetic)") + entity-type filter + entity-id search with labeled controls; pagination buttons have aria-labels; list refreshes every 20s (audit-view.tsx:76).
- **Bugs:** "1 events · page 1 of 1" (no pluralization, audit-view.tsx:160); verify-strip layout collapse (see Audit Verify below).

## Page: Audit Verify (button on `/audit`, not a route)

- **Status: WORKING functionally; layout BROKEN-ish (MEDIUM).**
- Live: click → 1.86s → "CHAIN VALID · 22 ROWS CHECKED · 22 CHAINED · 0 LEGACY" green strip with `role="status" aria-live="polite"`; broken-chain path renders a danger panel naming the first bad row (audit-verify-action.tsx:73-101, not triggered live).
- **Layout bug (MEDIUM):** when the strip renders, the SectionCard header collapses the title/description into an 82px-wide column (live geometry + `live-audit-verify.png`, reproduces from the earlier agent's `audit-verify.png` too — deterministic at 1440-1483px).
- **Scope communication gap (LOW):** verification spans BOTH environments by design (code comment lines 14-20) but the strip says only "22 ROWS CHECKED" while the visible stream shows 1 event; the explanation lives in a hover-only `title` tooltip.

## Page: Evaluation (`/evaluation` → redirects to `/research?tab=evaluation`)

- **Status: WORKING.** Redirect preserves `?run=` deep links (`app/evaluation/page.tsx:1-19`).
- **Content: the most information-dense page (4247px tall).** Stored-run table → run detail → Baseline-vs-PulseRecover comparison → holdout lift with CIs → per-stratum tables → methodology → detection/diagnosis/recovery/intervention-cost charts → per-incident diagnosis table → glossary.
- **Provenance: exemplary.** "Stored run row — the console never recomputes metrics"; "SAME SEEDED SCENARIO PER ARM · 4,893 FAILED PAYMENTS…"; methodology discloses priors, censoring, CI methods, and even the harness's own batch artifacts ("Sim-time action time-to-recovery is a batch artifact").
- **Decision-relevant content:** the stored run shows the gated loop UNDERPERFORMING both the naive retry baseline (₹16,744 vs ₹9,90,116 recovered) and the no-action holdout (13.7% vs 16.3%; ITT −2.6 PP) while holding the safety invariant (0 ungated actions vs 4,893). The UI presents this straight, no spin. (Efficacy question belongs to the ML/pipeline auditors; the UI's honest presentation is a strength.)
- **IA nit:** a 4000px single scroll with no section nav/sticky toc is heavy; "MTTR 0S" metric display reads as a bug without the methodology footnote.

## Page: Research (`/research`)

- **Status: WORKING.** Simulator banner (`role="note"`), ARIA-correct tabs, 5 scenarios with descriptions + per-scenario metric dropdown + Run; "Reset research data" behind two-step confirm (demo-control.tsx:190-224); run/reset summaries render real API responses; timeout treated as "still running server-side" rather than failure (lines 17-35, 62-65) — honest async UX.
- **Demo leakage (LOW):** scenario ids `payday_wave_demo`, `upi_outage_demo` render verbatim, contradicting the codebase's own rule that "the bare words 'simulation'/'demo' never appear user-visible" (`environment.ts:12-14`). Contained to the Research Lab, so impact is cosmetic.
- Run buttons NOT clicked (read-only rule) — scenario execution UNVERIFIED from this pass (verified by other audit tracks).

## Page: Settings (`/settings`)

- **Status: WORKING.** Connection card (env, connection state, masked key id `rzp_test_••••sMjo`, "KEY SECRET •••••••• NEVER LEAVES THE SERVER", webhook configured, auto sync, last sync/webhook timestamps), actions (Sync now / Test webhook / Disconnect), How-to-connect card (var names only, no values), Export card (CSV/JSON, env-scoped, "Environment: real_test" shown).
- **Secret hygiene: strong** — masked key id only; explicit "never leaves the server" copy.
- **Notable:** "Test webhook" is a NEGATIVE probe — sends a deliberately invalid signature; rejection = success (settings-view.tsx:295-308, comment line 343). Clever, but the button label doesn't hint at the inverted semantics; the outcome panel carries the explanation (LOW copy risk).
- **Disconnect (LOW):** single click, no confirm — but it only pauses auto-sync (`toggle.mutate(false)`), credentials untouched; the label "Disconnect" overstates the blast radius.

## Page: Not Found (404)

- **Status: WORKING.** "404 — page not found… Head back to the Command Center." + back button; chrome intact (`not-found.tsx` + screenshot). Doesn't set its own document title (trivial).

## Cross-cutting: Loading / Empty / Error / Success states

- **Loading: WORKING, consistent.** Skeletons everywhere with `aria-busy` + `aria-label` (data-table.tsx:59, audit timeline, incident detail, approvals queue); home skeleton preserves the target layout incl. KPI labels (`home-loading.png`).
- **Empty: WORKING, the dominant live state.** One shared `EmptyState` component (icon + title + description + optional CTA); copy is environment-aware (real vs research variants) and action-oriented (Sync now / Open Settings / Open Research Lab / Run a scenario). Weakest specimen is the most visible one — the home "Process your first test payment" (factually wrong for this account).
- **Error: WORKING.** Shared `ErrorPanel` with retry; API errors surface machine CODE + REQUEST ID (`incident-detail-missing.png`); mutation errors use `role="alert"`; global `error.tsx` boundary exists; scenario timeouts shown as "still running server-side" instead of false failures.
- **Success: WORKING but quiet by design.** Sync run summary (settings), "chain valid" strip (audit, `role="status"` aria-live), mutation outcome lines with StatusPill (approvals), policy-backtest report. Toast-free; state changes are confirmed inline. Reasonable for an ops console.
- **Polling model is visible to the user:** "updated just now · polls 15s" chips + "Backend components, polled every 20s" — refresh behavior is honestly disclosed.

## Cross-cutting: Mobile responsiveness

- **WORKING with one sore spot.** Breakpoint at `md` (768px): sidebar → hamburger drawer with correct dialog semantics; cards stack; filters wrap full-width; metric strips stack (`mobile-recovery.png`, `mobile-home.png`).
- **Sore spot (MEDIUM):** data tables keep desktop density — payments table is 1160px wide in a 322px scrollport (3.6× horizontal scroll; amount/status/error invisible by default). Scrollable (verified `overflow-x:auto` in DOM), not broken, but there's no scroll hint/shadow, so a phone user may never discover the other columns. No card-layout alternative for narrow screens.
- Chrome nits at 390px: top-bar badges wrap ("API · LIVE" dot wraps to its own line in `mobile-home.png`); 9.5px mono micro-labels are very small on phone screens.
- Round-1 mobile probe timeouts were cold-start flake; round 2 rendered fine (HTTP 200).

## Cross-cutting: Provenance labeling (real vs simulated vs research data)

- **WORKING — the strongest aspect of this UI.** Env toggle in chrome (persisted, localStorage); 4 simultaneous signals in research mode (top strip, header badge, description, chip); per-page chips ("Razorpay Test Mode · 1h window · 0 records" / "Synthetic Research Dataset"); per-row source badges on payments ("RAZORPAY TEST"); per-row env badges on audit ("REAL TEST"); audit copy states the never-mix rule; evaluation labels stored runs and seeded arms; settings shows masked key id + "Environment: real_test" on exports.
- Label vocabulary is centralized in `environment.ts` and honored except the `_demo` scenario ids (LOW).
- **The one structural gap:** the "0 RECORDS" on home counts event-stream records in a 1h window, while "6 RECORDS" on payments counts all-time synced rows — same chip component, different semantics, nothing tells the user (feeds the HIGH home finding).

## Cross-cutting: Financial-safety affordances

- **WORKING (source-verified; live execution not exercised per read-only rule).**
- Two-step confirms on every mutation reachable without data: reconciliation sweep, policy backtest, research reset, build-opportunities (with focus moving to Confirm); execution uses `role="alertdialog"`.
- Approval discipline: Reject/Escalate blocked until a reason is typed (disabled state + explanatory tooltip); policy gate verdict + version + matched rules + human-readable reasons shown per pending item; UNKNOWN outcomes resolved by re-querying gateway truth, with copy promising the charge is never re-fired blindly.
- Honesty affordances: confidence intervals + "low confidence" badge on revenue-at-risk; "SAFETY INVARIANT HELD" check on evaluation; "llm provider none · disabled" in system health; audit hash-chain verify one click away with request ids on errors.
- Minor: Settings "Disconnect" has no confirm (but only pauses sync); "Test webhook" is an inverted-semantics probe whose label doesn't say so.

## Cross-cutting: Accessibility (a11y)

- **Mostly strong (verified live + source):** skip link; `main` landmark; `nav[aria-label="Primary"]`; `aria-current="page"`; every icon `aria-hidden`; 0 unnamed buttons / 0 unlabeled selects / 0 imgs without alt (live DOM count); aria-live on pagination count, verify strip, status pill; visible 2px amber focus outline on all 14 sampled Tab stops in logical order; ARIA-correct tabs (roving tabindex + arrow keys) and modal nav drawer (`role="dialog" aria-modal`); status communicated by text, never color alone (pills/badges all labeled).
- **Gaps:**
  - **No h2/h3 anywhere (MEDIUM)** — one h1 per page, all card titles are non-heading markup (live: `h2count: 0`); screen-reader heading navigation is impossible.
  - **`title`-attribute tooltips carry unique information (LOW-MEDIUM)** — full payment ids, full error text, audit-verify scope, health-poll source are hover-only; keyboard and touch users can't reach them.
  - Danger red on dark = 4.73:1 (marginal AA pass); 9.5px mono micro-labels pass contrast (5.58:1) but are tiny.
  - No reduced-motion check performed (UNVERIFIED); the status pulse animation is reserved for degraded states by design.

---

## The 12 first-time-user questions

Judged against the LIVE deployment state (6 payments, 0 incidents, 0 recoveries) — empty states are the real first impression.

1. **"Do I understand what this is in 10 seconds?" — YES.** Top bar "Payment Reliability & Revenue Recovery Engine" + h1 "Command Center" + one-sentence description naming revenue at risk, reliability, recovery pipeline (`home.png`). Brand, purpose, and data source are all above the fold.
2. **"Is the data real?" — YES.** Provenance chips with record counts, per-row "RAZORPAY TEST" source badges, masked key id in Settings, "the console never recomputes metrics" on evaluation. Nothing rendered claims to be something it isn't (probe D/E).
3. **"Is test mode visible?" — YES, unavoidable.** "RAZORPAY TEST MODE · CONNECTED" pinned top-right on every page + per-page chips + Settings environment row (every screenshot).
4. **"Can I find the revenue-at-risk?" — NO.** The Revenue Hero exists only behind a non-empty environment (`command-center-screen.tsx:192-194`); live, the entire concept is replaced by the empty panel. A first-time user sees the words "Revenue at risk" in the subtitle but no number, no hero, no entry point — and 6 real payments one click away don't unlock it.
5. **"Do I understand WHY (why empty / why at risk)?" — NO.** The empty state says what to do ("Process your first test payment") but not why the dashboard disagrees with the Payments page; the event-window semantics are nowhere explained. For the "why is this revenue at risk" question there is simply no live incident to learn from.
6. **"Can I see what the AI thinks?" — NO (in current state).** Tagline "AI proposes · Policy decides" and recovery copy explain the division of labor, and System Health honestly shows "llm provider none · disabled", but no diagnosis/insight surface has content live. (Incident detail + investigation panel exist in code — UNVERIFIED with data.)
7. **"Is it clear what action I can take?" — YES.** Sync now (home + settings), Run scenario (research), Run reconciliation / backtest (recovery), Verify integrity (audit), filters, export. CTAs are consistent and captioned with what they'll do.
8. **"Do I know what I'm allowed to do (who decides)?" — YES (concept) / NO (instance).** Recovery page + approval center explain the policy gate in plain language and the policy version is visible in System Health; but no live policy decision exists to inspect, so the gate's behavior is only described, not shown. Net: PARTIAL → scored **YES-** (concept clear).
9. **"Can I tell what happened?" — PARTIAL → NO-.** Audit trail shows 1 system event (detection.run); payments shows 6 synced rows; but the home timeline/chart/recent-activity surfaces are all empty, so the cross-page story of "what happened on my account" doesn't cohere (the 6 payments are invisible to every dashboard surface).
10. **"Can I verify recovered money?" — NO.** Nothing recovered exists; the verification path (RECOVERED only via webhook/inline verification, CI badging, audit rows) is documented in copy but cannot be exercised or seen on any live row.
11. **"Is there an audit trail?" — YES.** Dedicated nav item, append-only + env-scoping copy, hash-chain "Verify integrity" that returns CHAIN VALID live in <2s, request ids on errors, raw JSON details preserved.
12. **"Can I tell research from merchant data?" — YES, exemplary.** Two-env chrome toggle, 4 simultaneous research-mode signals, per-row env badges, audit env filter, "never mix" copy, isolated reset (probe E; `home-research-env.png`; research-view.tsx:69-82).

Score: 6 YES, 1 YES-, 5 NO/NO-. The NOs cluster on one root cause: the real_test analytics surface is starved of the webhook event stream, so every "show me" question (revenue at risk, AI thinking, recovery verification, what happened) is unanswerable in the current deployment.

---

## Findings (severity-tagged)

- **CRITICAL — none.** No UI defect blocks a destructive or money-moving action; no secret leakage in UI (masked key id only); no provenance confusion between synthetic and merchant data.
- **HIGH-1 — Home contradicts Payments; entire analytics surface hidden.** Live home: "No payment activity yet — Process your first test payment" + "0 RECORDS" while /payments lists 6 real payments (₹750-₹1,299) + "6 RECORDS". Revenue hero, KPI strip, 24h chart, recent incidents, recovery pipeline all unrendered. Structural cause: dashboard reads webhook-born `payment_events` in a 1h data-anchored window; sync writes Payment rows only. Evidence: `home.png` vs `payments.png`; `command-center-screen.tsx:82-98,154-170`; `dashboard.py:156-167`; `webhook_handlers.py:299` (sole PaymentEvent constructor). First impression for any real_test account whose events don't flow through webhooks.
- **HIGH-2 — The only populated "product result" a visitor can find shows the product losing.** Research → Evaluation stored run: gated loop recovers ₹16,744 vs baseline ₹9,90,116 (−₹9.7L) and trails the no-action holdout 13.7% vs 16.3% (ITT −2.6 PP). UI presents it faithfully (strength), but as a first impression the single piece of non-empty evidence argues against efficacy; safety invariant (0 ungated actions) is the counter-narrative. Evidence: probe D live text; `evaluation.png`. (Flagged for synthesis; not a UI defect.)
- **MEDIUM-1 — Audit verify strip collapses the card header.** After "Verify integrity", title/description squeezed to an 82px-wide column at 1440-1483px desktop. Deterministic, reproduced in two independent captures (`live-audit-verify.png`, `audit-verify.png`; geometry: title 82×34, desc 82×80). Cause: `section-card.tsx:32-38` header flex + `shrink-0` actions + `basis-full` strip.
- **MEDIUM-2 — Payments table overflows at all audited widths.** 1160px table vs ~1136px content @1440px (CREATED column clipped, no scroll affordance — `payments.png` shows 7 of 8 columns); 3.6× viewport horizontal scroll @390px with STATUS/AMOUNT/ERROR off-screen by default (`live-mobile-payments.png`, probe F2).
- **MEDIUM-3 — No heading hierarchy.** Live DOM: 1 h1, 0 h2/h3 on the home page (card titles are non-headings; `page-header.tsx:23` is the only h1 source). Screen-reader users get no section navigation.
- **LOW-1 — "1 events" pluralization** (`audit-view.tsx:160`, visible in `audit.png`).
- **LOW-2 — `_demo` scenario ids violate the project's own label rule** (`environment.ts:12-14` vs `research.png`).
- **LOW-3 — Verify-scope communication:** chain spans both environments (22 rows) while the stream shows 1; explanation only in a hover tooltip (`audit-verify-action.tsx:14-20,36`).
- **LOW-4 — Hover-only `title` tooltips carry unique info** (full payment ids, full error descriptions, tooltips on disabled approve-reject buttons) — unreachable for keyboard/touch users (`payments-view.tsx:34,49,89`).
- **LOW-5 — Settings "Disconnect"**: one click, no confirm, label overstates effect (only pauses auto-sync; `settings-view.tsx:309-318`).
- **LOW-6 — "Test webhook" inverted semantics** (success = rejection of an invalid signature) not hinted in the label (`settings-view.tsx:295-308`).
- **LOW-7 — Danger red 4.73:1** (marginal AA pass) and pervasive 9.5px micro-labels.
- **INFO — Render free-tier cold starts** caused probe-visible multi-second first loads (round-1 verify >20s; round-1 mobile 3×60s failures, then HTTP 200 + full render in round 2). Expect first-visit spinner/skeleton durations well beyond the 15s poll copy's implication.

## Unverifiable / Not verified

- Populated states for: incident list rows, incident detail (diagnosis, investigation panel, segment breakdown, metric chart), recovery pipeline rows, opportunity drawer, strategy panel + execution `alertdialog`, approval decision flow (Approve/Reject/Escalate outcomes), backtest report, reconciliation report. Reason: 0 incidents/opportunities live; read-only rule forbade seeding research scenarios or executing actions.
- "Sync now", "Test webhook", "Disconnect", "Download CSV", scenario "Run", "Reset research data" — not clicked (mutating). Their result panels are source-verified only.
- Mobile horizontal-scroll gesture (DOM-verified scrollable; not gesture-tested), reduced-motion behavior, actual screen-reader output, `prefers-*` handling.
- Whether the live deployment EVER renders the non-empty home in real_test (would require webhook terminal events; the payments synced via REST don't generate them — consistent with the mission's verified context that real_test "barely has" an event stream).
- Round-1 mobile total load failures: transient cold-start; not reproducible in round 2. UNCERTAIN how often a first-time mobile visitor hits a >3-minute blank.

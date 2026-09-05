# Phase B — Closed-Loop Learning + Recovery Intelligence

Agent: learning-loop analyst | Date: 2026-09-02 | Repo: D:/Razorpay @ main
Status vocabulary per candidate: EXISTING / POSSIBLE / HIGH-VALUE / SPECULATIVE / UNSAFE / LOW-VALUE → BUILD NOW / BUILD LATER / RESEARCH ONLY / REJECT.
Read-only: no production code modified; every claim carries path:line evidence. Unverifiable → UNCERTAIN.

Question assigned: can intervention → outcome → evidence → future-decision become a genuine **measured** learning loop — per-action-class measured conversion feeding opportunity ranking — **without** re-introducing circular priors and **without** decorative ML?

Short answer: **Yes, and the evidence vertex is now implemented.** The outcome side is measured (harness outcome model, webhook-verified RECOVERED actions, holdout organic rates). The new read-only `GET /api/v1/recovery/outcome-rates` surface aggregates realized action outcomes per `(action_type × failure_class)`, pairs them with an environment-scoped organic baseline, and reports Newcombe/Wilson incremental cells with provenance. The decision side still uses the documented prior tables: no measured rate changes ranking or policy authorization yet. The next decisioning slice must preserve the MIN_CELL fallback and only use conclusive, same-environment evidence.

---

## 1. Observations (shared evidence base)

### 1a. Measured data that already exists

1. **Measured outcome model (harness).** `backend/app/services/evaluation/outcomes.py` fits `OutcomeModel` on each arm's scratch DB *after* `run_simulation`, *before* any recovery action (`measure_outcomes`, outcomes.py:171-178; call sites runner.py:606, 658). Per-class `retry_success`, per-class `organic_return`, pooled `self_resolution` + lag distribution, `MIN_CELL = 30` pooled fallback (outcomes.py:81, 157-168). Provenance is labeled `measured_from_simulator_behavior` (outcomes.py:115) and 4 residual anchoring assumptions are recorded verbatim on every run (ASSUMPTIONS, outcomes.py:85-99). Persisted per arm (`runner.py:633, 693`), at top level (`runner.py:562`), and into the experiment config as `outcome_model_measured` (`runner.py:510`). This is the DEF-03 fix: the hand-set CONVERSION table is gone from the outcome path (`runner.py:24`).
2. **RECOVERED action outcomes (real executor path).** `recovery_actions` rows carry terminal webhook-verified states — RECOVERED / FAILED / UNKNOWN — with `action_type`, `amount_paise`, `verified_at`, `completed_at`, `confidence`, `strategy_id`, `opportunity_id` (`backend/app/models/recovery.py:85-130`). Verification is real: `payment_link.paid` amount/currency/partial cross-check before RECOVERED (`webhook_handlers.py:394-430` per flows-recovery-evaluation.md:151). Both `RecoveryOpportunity` and `RecoveryAction` carry `EnvironmentMixin` (models/recovery.py:21, 85), so measured rates can be computed **per environment** — the research/real_test isolation the gate proved (phase-a-release-gate.md:14) extends to any learning artifact.
3. **Aggregated measured recovery.** `RevenueService.recovered_revenue` still sums RECOVERED actions by window and by action_type (engine.py:319-365); `_actual_recovered` remains per incident (engine.py:512-521). The Phase-B `services/recovery/outcomes.py` service now adds per-(action, class) rates, UNKNOWN counts, Wilson intervals, same-environment organic cells, and Newcombe incremental cells. UNKNOWN remains separate and never enters the conversion denominator.
4. **Holdout organic rates.** Pre-registered deterministic customer-level holdout (holdout.py:47-64, fraction 0.10 at holdout.py:35) with ITT lift and Newcombe CIs (holdout.py:89-103), plus class-adjusted lift and per-stratum rates (flows-recovery-evaluation.md:229). Organic `no_action` self-resolution is measured on both groups through the real webhook path (flows-recovery-evaluation.md:229). Current canonical result: **+0.59 pp ITT, CI crosses zero, labeled inconclusive** (phase-a-release-gate.md:45) — accepted truth, not re-litigated here.
5. **Statistics utilities for honest small-n rates already exist.** `wilson_interval` with degenerate-input honesty (holdout.py:72-86), `rate_confidence` + `wilson_interval` in `app/services/revenue/statistics` (imported engine.py:39), and the `Estimate` honesty contract (`low_confidence`, `basis` string, `point=None` when no defensible point — types.py:14-64).

6. **Durable observation record (B3).** `recovery_outcome_observations` is the
	immutable evidence layer. It records `action_id`, `opportunity_id`, action
	type, observed outcome, environment, `decision_at`, `observed_at`, policy
	decision/version, gateway request id, source, and compact verification
	evidence. The unique key `(action_id, observed_status)` makes repeated
	webhook delivery, reconciliation, worker retries, and repeated reads
	idempotent. A `FAILED -> RECOVERED` lifecycle intentionally produces two
	observations with distinct outcome states and timestamps; the original
	decision time is never rewritten.

### 1b. Where hand-set priors still decide live behavior

1. **Recoverability table** — `RevenueConfig.recoverability` (config.py:68-89): TIMEOUT 0.70, SOFT_DECLINE 0.60, ABANDONMENT 0.35, INSUFFICIENT_FUNDS 0.20, HARD_DECLINE 0.05, UNKNOWN 0.10. Module docstring: "a documented, deliberately conservative prior — not a measured fact" (config.py:3-4). Consumed by `opportunity_estimate` (engine.py:273) and `revenue_at_risk` recoverable allocation (engine.py:487-490).
2. **Strategy-effectiveness table** — config.py:94-113 (RETRY 0.50, LINK 0.30, NOTIFY 0.15, …). Consumed at engine.py:296-302; the product `amount × recoverability × effectiveness` becomes `expected_recovery_paise`, which is the **ranking key** in `StrategyGenerator` (strategies.py:146-154) and the backfilled opportunity planning summary (strategies.py:178-183).
3. **Action-fit tables** — `_RETRY_FIT` / `_LINK_FIT` / `_NOTIFY_FIT` (strategies.py:57-80), feeding `confidence = evidence_strength × action_fit` (strategies.py:166). Fit priors touch the policy gate (confidence floor 0.85), not just ranking — so they are safety-adjacent and a worse first target than the pricing tables.
4. Per-opportunity estimates are honestly labeled today: full `[0, amount]` band, `low_confidence=True` always, `prior_confidence=0.3` (engine.py:275-285, config.py:60) — "these numbers rank strategies, they do not promise revenue" (engine.py:243-247).

### 1c. The circularity boundary (what "not circular" means here)

- The DEF-03 lesson: an outcome decided by a hand-set table makes the measurement vacuous (outcomes.py:1-15; the pre-fix stored run's −2.55 pp lift was "fully determined by those tables"). The mirror-image mistake in Phase B would be **prior laundering**: letting the simulator's hand-built customer behavior (via `OutcomeModel` rates) rank real merchant actions and calling it "measured". `OutcomeModel` is legitimate *inside the harness* (both arms share it); it is not evidence about real traffic.
- The honest loop: **realized action outcomes** (executor → gateway → signed webhook → RECOVERED/FAILED) aggregated per (action_type × failure_class), **net of the class-matched organic rate**, fed back into the planning tables with n-gated fallback. Measurement comes from the system's own interventions, not from a model of the customer.
- Selection-bias caveat: actions are taken on opportunities the current policy already favored, so raw measured conversion is conditioned on the current policy. Subtracting the organic baseline (holdout) and keeping Wilson CIs wide at small n are the mitigations available without randomized exploration — which is rejected below (C9).

### 1d. Data-availability reality check

- **real_test**: 6 synced payments, 0 opportunities, 0 pending approvals (live probes, flows-recovery-evaluation.md:271). A measured table there is **years away at current traffic** — any design must be research-env-first with automatic prior fallback for real_test.
- **research env**: actions accrue via demo/research-lab usage in the main DB. Evaluation-harness actions are richer (100 interventions in the stored run) but **die with the deleted scratch DBs** — only aggregates persist (flows-recovery-evaluation.md:251, 280). The outcome *generator* is persisted (runner.py:633, 693); the realized per-action outcome matrix is not. That is a fixable evidence leak (C2).
- `delayed_retry` is currently unexercised in the batch harness (SCHEDULED never fires within a run — outcomes.py:40-44), so the payday-effect prior (`_DELAY_BONUS_INSUFFICIENT_FUNDS`, strategies.py:86) cannot be validated yet. Disclosed in code, not silent.

---

## 2. Candidates

| # | Candidate | Classification | Recommendation |
|---|---|---|---|
| C1 | Measured action-outcome aggregation service (per env, action_type × failure_class, Wilson CI, MIN_CELL fallback) | HIGH-VALUE | **IMPLEMENTED — EVIDENCE ONLY** |
| C2 | Persist per-run action-outcome matrix in evaluation run metrics (scratch DB evidence leak) | HIGH-VALUE | **IMPLEMENTED — HARNESS EVIDENCE ONLY** |
| C3 | Measured-rate override of the recoverability×effectiveness product in `opportunity_estimate`, env-scoped, prior fallback + honesty label | HIGH-VALUE | **BUILD NOW** (research env first) |
| C4 | Organic-baseline subtraction (incremental, not raw, conversion as the feedback signal) | HIGH-VALUE | **BUILD NOW** (bundled with C3) |
| C5 | Cross-run drift monitoring of measured rates | POSSIBLE | BUILD LATER |
| C6 | In-harness closed-loop validation experiment (run N outcomes → run N+1 ranking, pre-registered delta) | POSSIBLE | BUILD LATER |
| C7 | Import harness `OutcomeModel` rates into live decisioning | UNSAFE (prior laundering) | **REJECT** |
| C8 | Learned propensity model P(recover \| features) | SPECULATIVE | REJECT for Phase B |
| C9 | Bandit/Thompson exploration on live actions | UNSAFE | **REJECT** |
| C10 | Real-test-first measured table (skip research env) | SPECULATIVE | REJECT (subsumed by C3 fallback) |

---

## C1 — Measured action-outcome aggregation service

- **Evidence**: terminal action rows exist with full schema (models/recovery.py:85-130); failure class derivable via `classify_failure` on the linked payment (engine.py:258-261 pattern) with the opportunity-type fallback (engine.py:262-271); sums-by-type already computed (engine.py:348-354) — the missing piece is rates with denominators and CIs. Wilson/confidence utilities exist (holdout.py:72-86; engine.py:39). Environment scoping is free via `EnvironmentMixin` (models/recovery.py:21, 85).
- **Implementation concept**: one read-only service (sibling to `RevenueService`, e.g. `services/recovery/outcomes.py`) computing, per environment and per (action_type × failure_class) cell: n_executed, n_recovered, n_failed, n_unknown (excluded from the rate, surfaced), Wilson CI, and `low_confidence` when n < MIN_CELL (30, the outcomes.py:81 precedent). One GET endpoint (e.g. `/api/v1/recovery/outcome-rates`) returning cells + provenance (`"measured_from_action_outcomes"`, window, environment). No decisioning change — this is the evidence vertex made queryable.
- **Dependencies**: none new. Reuses `classify_failure`, `wilson_interval`, the `Estimate` honesty idiom.
- **Risks**: mis-attribution of class for opportunities without payments — mitigated by reusing the exact class-resolution order of `opportunity_estimate` (engine.py:255-271). UNKNOWN outcomes inflating denominators — mitigated by counting them separately, matching engine.py:341-346 semantics.
- **Test strategy**: unit tests over seeded recovery_actions (all-terminal mix, UNKNOWN exclusion, cell below MIN_CELL → `low_confidence`, environment isolation — research rows never leak into real_test rates, mirroring the gate's isolation proofs phase-a-release-gate.md:14). Empty-DB test returns zero cells, not a crash.
- **Demo value**: high — a visible "measured conversion" matrix next to the prior table; the first artifact that makes the loop *inspectable*.
- **Complexity**: S (~200 LOC + tests; read-only).
- **Recommendation**: **BUILD NOW** — every other build candidate depends on it.

## C2 — Persist per-run action-outcome matrix in evaluation metrics

- **Evidence**: harness actions' per-action records die with the scratch DB (flows-recovery-evaluation.md:251, 280); only aggregate arm metrics persist; `opportunity_types` persistence (runner.py:968-984 per flows doc I.6) is the established pattern for "persist it in run metrics so it is machine-checkable".
- **Implementation concept**: in the PULSECOVER arm teardown, group terminal actions by (action_type × failure_class) and persist counts (executed/recovered/failed/unknown) into run metrics — same additive style as `opportunity_types` ("older runs simply lack the key"). Label provenance `measured_in_harness`.
- **Dependencies**: none; touches runner metric assembly only.
- **Risks**: consumers confusing harness-measured rates with real-traffic rates — mitigated by the provenance label and by keeping this matrix out of C3's live override input (research-env DB rows only; harness matrices are evidence exhibits, not decision inputs).
- **Test strategy**: extend the existing persistence test pattern (`backend/tests/evaluation/test_opportunity_types.py:17-28` per flows doc I.6): run a small scenario, assert the matrix key exists and its totals reconcile with the arm's aggregate intervention/recovered counts.
- **Demo value**: medium-high — every stored run becomes an evidence deposit; enables a "rates converging across runs" chart later.
- **Complexity**: S.
- **Recommendation**: **BUILD NOW** — stops an evidence leak for a few lines.

## C3 — Measured-rate override of recoverability×effectiveness in `opportunity_estimate`

- **Evidence**: the prior product lives at engine.py:273 (recoverability) and engine.py:296-302 (effectiveness); ranking consumes its output (strategies.py:146-154). The fallback idiom exists (`opportunity_class_defaults`, engine.py:262-271; MIN_CELL pooled fallback, outcomes.py:157-168). `RevenueConfig` is already injectable per-merchant (config.py:5-6, engine.py:121-123) — a measured override needs no signature changes.
- **Implementation concept**: a measured-rates provider (built on C1) that, per failure class and per action, returns `P(capture | action, class)` when the cell's n ≥ MIN_CELL, else `None`. In `opportunity_estimate`: measured cell present → use it in place of the `recoverability × effectiveness` product, set `basis="measured: n=… Wilson [lo..hi] (research)"` and confidence from `rate_confidence(n)`; absent → today's prior path, byte-identical, `basis` unchanged. Environment gate: measured rates computed from the **same environment** as the opportunity; real_test cells will be empty for the foreseeable future (1d) so real_test automatically stays on priors — no flag day, no config. Surface the label in the plan API payload so the UI can render "measured (n=87)" vs "prior".
- **Dependencies**: C1 (rates), C4 (organic subtraction — see risks), existing `RevenueConfig` injection.
- **Risks**: (1) **Selection bias** — measured conversion is conditioned on the current policy's targeting; ranking changes shift the mixture, shifting the measured rates (feedback). Mitigation: C4's organic subtraction + slow update cadence (recompute on read from the full window, not incrementally per action) + Wilson-honest small cells. (2) **Circular ranking** — if measured rates merely re-rank among the same 6 candidates, worst case is a different approval queue, not unsafe execution: the policy gate is untouched and still authorizes every action (policies/default.yaml:2-3; flows doc G.7). Confidence inputs (fit tables) are deliberately NOT learned in this candidate — the 0.85 floor math is unchanged. (3) **Distribution shift** — a measured rate from last month's incident mix may misprice today's; mitigated by windowing (e.g. trailing 30 days of terminal actions) and C5 later.
- **Test strategy**: (a) regression: with zero terminal actions, `opportunity_estimate` output is byte-identical to today for all opportunity types; (b) seeded actions above MIN_CELL flip the basis string and the point; (c) below-MIN_CELL cell falls back to prior; (d) environment isolation: research measured rates never apply to a real_test opportunity and vice versa; (e) ordering assertion: tests that assert the recoverability ordering contract (config.py:64-67) keep passing on the prior fallback path.
- **Demo value**: highest of the set — the Evaluation Lab / opportunity plan can show prior vs measured side-by-side with n and CI, and the ranking visibly responds to measured evidence in the research env while real_test honestly reports "insufficient data — documented priors". This *is* the closed loop, demoable without any ML.
- **Complexity**: M (provider + engine branch + plan serialization + tests).
- **Recommendation**: **BUILD NOW** (research env first; real_test inherits automatically when n accrues). This is the assignment's "(b)" and the strongest candidate — see §4.

## C4 — Organic-baseline subtraction (incremental conversion as the feedback signal)

- **Evidence**: raw action conversion counts customers who would have paid anyway — the holdout exists precisely to separate this (holdout.py:1-21); per-stratum organic rates are already computed per run (flows doc I.3, runner.py:1239-1273 per flows doc); Newcombe CI for a difference of proportions exists (holdout.py:89-103).
- **Implementation concept**: the rate C3 feeds into ranking is **incremental** = P(recovered \| action, class) − P(organic recovery \| class, no action), with a Newcombe CI; when the CI brackets zero, the cell is labeled `inconclusive` (the exact chip semantics the gate already proved, phase-a-release-gate.md:16) and the prior stands. In the research env the organic rate comes from no_action outcomes / holdout-organic observation; outside the harness (main DB research traffic) it comes from `no_action`-strategy outcomes and unacted opportunities that self-resolved — measurable from payment chains, same walk as outcomes.py:197-217.
- **Dependencies**: C1.
- **Risks**: organic baselines outside the harness are not randomized (no holdout in production research traffic), so the subtraction is class-matched but not experiment-grade — disclosed in the basis string. Subtraction can go negative; clamp at 0 for ranking but display the signed value (honesty over tidiness).
- **Test strategy**: synthetic cells with known action/organic rates → incremental point and CI correct; CI-brackets-zero → `inconclusive` label + prior retained; negative incremental clamped for ranking, signed in the payload.
- **Demo value**: high — "we rank by *incremental* recovery, not attribution" is a principled line that directly answers the demo-judge question "how do you know you caused it?".
- **Complexity**: S (arithmetic + labels on top of C1).
- **Recommendation**: **BUILD NOW**, bundled with C3 — it is the methodological core that keeps the loop from learning to maximize attribution instead of value.

## C5 — Cross-run drift monitoring of measured rates

- **Evidence**: per-run outcome models are already persisted (runner.py:562, 633, 693); with C2, per-run realized matrices persist too. No cross-run comparison exists.
- **Implementation concept**: a small aggregation over stored runs (read-only, like `GET /evaluation/metrics`, evaluation.py:78-115) reporting per-class rate trajectories across seeds/anchors and flagging cells whose Wilson intervals no longer overlap their trailing median.
- **Dependencies**: C2 (for realized matrices); stored runs.
- **Risks**: alert fatigue at small n — gate flags on interval non-overlap, not point movement.
- **Test strategy**: fabricate runs with shifted rates → flag fires; identical runs → no flag.
- **Demo value**: medium — supports a "the system watches its own learning for drift" panel.
- **Complexity**: M.
- **Recommendation**: BUILD LATER (after C1-C4 land and multiple runs exist).

## C6 — In-harness closed-loop validation experiment

- **Evidence**: the harness is the only place with a counterfactual (holdout) and reproducible two-arm runs (phase-a-release-gate.md:15 — two identical canonical runs, 278 metric leaves identical). Strategy generation is deterministic given the estimate inputs.
- **Implementation concept**: pre-registered experiment: run A ranks with priors; persist C2 matrix; run B (same seed family, different anchor) ranks with C3/C4 measured rates; compare recovered revenue, interventions, unsafe count, and holdout lift. The honest label: in-harness the learnable ceiling is the OutcomeModel's own rates (the world is closed), so this validates the *machinery* of the loop, not real-world efficacy — say so in the run notes, same style as existing disclosures (runner.py:16-43).
- **Dependencies**: C1-C4.
- **Risks**: overclaiming — the closed-world caveat must ship in-band with the results. Harness measured rates converging to the outcome generator is expected, not a bug; the falsifiable claims are "ranking changes", "no unsafe actions", "evidence artifacts persist".
- **Test strategy**: determinism (same seed → same matrices), isolation counters stay 0/0, unsafe_action_count stays 0.
- **Demo value**: highest *narrative* value for Phase B — "we closed the loop and measured the difference" — but only credible after C1-C4 exist.
- **Complexity**: M.
- **Recommendation**: BUILD LATER — the Phase-B capstone once the plumbing ships.

## C7 — Import harness `OutcomeModel` rates into live decisioning — REJECT

- **Evidence**: `OutcomeModel` is measured from simulator customer behavior (outcomes.py:17-34, provenance outcomes.py:115); the simulator's mechanisms are hand-built constants (CHECKOUT_RETRY_RATE, LATE_CAPTURE_RATE — outcomes.py:20-34).
- **Implementation concept**: n/a (rejected).
- **Dependencies**: n/a.
- **Risks**: this is the circular-prior reintroduction the assignment forbids: hand-set simulator behavior becomes "measured" truth ranking real merchant actions — prior laundering. It would silently re-couple live decisioning to the very tables DEF-03 removed (outcomes.py:1-15).
- **Test strategy**: n/a.
- **Demo value**: negative if discovered — it undoes the audit's central credibility win.
- **Complexity**: trivial to do, which is exactly why it must be rejected explicitly.
- **Recommendation**: **REJECT**. `OutcomeModel` rates are legitimate only inside the harness, where both arms share them (outcomes.py:12-15).

## C8 — Learned propensity model P(recover | features) — REJECT for Phase B

- **Evidence**: the repo's one ML model carries a full governance apparatus — temporal splits, pre-registered selection, calibration review, sha256-pinned datasets, disclosed gate override (ml-audit.md §4-6, §9). Measured ECE on the shipped diagnosis model is 0.1291 on prod frames (ml-audit.md:130). Cell counts for a recovery propensity target would be far thinner (research-env actions in the main DB number in the dozens-to-hundreds, not thousands; real_test ≈ 0 — §1d).
- **Implementation concept**: n/a (rejected for Phase B).
- **Dependencies**: C1 (as future training-data source) if ever revisited.
- **Risks**: decorative ML — a model trained on tens of outcomes per cell is strictly less honest than the same cells as Wilson-bounded rates; it would import the diagnosis model's entire governance burden (splits, calibration, drift, leakage audits) for a ranking signal that a lookup table already provides.
- **Test strategy**: n/a.
- **Demo value**: superficially attractive ("we added ML") but fragile under questioning; the measured-rates story is stronger precisely because it is inspectable.
- **Complexity**: L (to do honestly).
- **Recommendation**: **REJECT for Phase B**; revisit only when C1 shows per-cell n in the high hundreds — at which point it is a calibration problem, not a Phase-B one.

## C9 — Bandit/Thompson exploration on live actions — REJECT

- **Evidence**: every action is a real gateway mutation or customer contact, gated by a total deterministic policy engine (policies/default.yaml:2-3; flows doc G.5/G.7); stopping rules, cooldowns, and per-customer daily caps exist precisely to bound action frequency (policies rules 9-14, flows doc G.5).
- **Implementation concept**: n/a (rejected).
- **Dependencies**: n/a.
- **Risks**: exploration spends merchant money and customer goodwill on deliberately sub-optimal actions; it interacts with stopping rules and cooldowns in ways that would need re-proof of the safety invariants (unsafe_action_count, one-mutation-per-action). It also breaks the "policy engine is the ONLY component that may authorize" boundary in spirit: exploration is a probabilistic decider.
- **Test strategy**: n/a.
- **Demo value**: none that survives scrutiny.
- **Complexity**: L.
- **Recommendation**: **REJECT**. The C4 organic-subtraction design gets most of the de-biasing benefit without exploration.

## C10 — Real-test-first measured table — REJECT (subsumed)

- **Evidence**: real_test has 6 payments and 0 opportunities (flows-recovery-evaluation.md:271); detection consumes an event stream real_test barely produces (flows doc H.11 starvation caveat, line 201).
- **Risks**: building the loop on real_test first means building it on ~zero data; every cell would be `low_confidence` indefinitely.
- **Complexity**: same as C3 but with no payoff horizon.
- **Recommendation**: **REJECT** as a standalone plan — C3's environment-scoped fallback already gives real_test measured rates automatically the day n accrues.

---

## 3. Cross-cutting risks (c) — severity-tagged findings

1. **[HIGH] The learning loop is open at exactly one joint.** Measured outcomes exist on three independent tracks (harness OutcomeModel, terminal recovery_actions, holdout organics), but nothing aggregates realized action outcomes per (action × class) or feeds them back — three hand-set prior tables still price and rank every opportunity (config.py:68-113 → engine.py:273, 296-302 → strategies.py:146-154). C1+C3+C4 close it with patterns the repo already ships.
2. **[HIGH] Prior laundering is the live temptation.** The single most dangerous Phase-B move is importing harness `OutcomeModel` rates into live decisioning (C7): simulator customer behavior would silently become "measured" truth for real merchants — re-creating the DEF-03 circularity one layer up. Rejected above; worth a one-line ADR so the rejection is on record.
3. **[MEDIUM] Selection-bias feedback.** Measured conversion is conditioned on what the current policy chose to act on; without organic subtraction (C4) and wide small-n intervals, the loop learns to maximize *attribution*, not recovery. With C4, the residual bias is disclosed, not eliminated — production research traffic has no randomized holdout.
4. **[MEDIUM] Small-n reality.** real_test cannot feed a measured table at current traffic (6 payments, §1d); research-env cells will clear MIN_CELL=30 only after sustained demo usage. The design must treat "stay on priors with an honest label" as a normal state, not a failure — the fallback is the feature.
5. **[MEDIUM] Harness evidence leak.** Realized per-action outcomes from evaluation runs die with the scratch DBs (flows doc:251, 280); only the outcome *generator* persists. C2 fixes this for a few lines and should land with C1.
6. **[LOW] Delayed-retry effects are currently unmeasurable** (batch harness never fires SCHEDULED — outcomes.py:40-44), so the payday prior (strategies.py:86) stays a prior until a wall-clock-capable harness path exists. Fine to defer; must stay disclosed.
7. **[LOW] Don't learn the fit tables (yet).** `_RETRY_FIT/_LINK_FIT/_NOTIFY_FIT` feed `confidence = evidence × fit` (strategies.py:166), which interacts with the 0.85 auto-execute floor — learning them is safety-adjacent. The pricing tables (C3) move ranking without touching gate confidence; keep it that way in Phase B.

## 4. Verdict

**CURRENT SLICE:** C1 + C4 are implemented as a read-only evidence API, B3
adds durable, idempotent outcome observations, and C2 persists the evaluation
harness matrix with explicit `measured_in_harness` / `research` provenance. No
ranking or policy behavior changes yet. **BUILD NEXT:** a separately gated C3
decision-support contract only when same-environment cells have sufficient
sample size and a non-zero incremental CI. **BUILD LATER:** C5 drift monitoring,
C6 in-harness closed-loop validation
(the Phase-B capstone demo). **REJECT:** C7 (prior laundering), C8 (decorative
ML at this n), C9 (unsafe exploration), C10 (no data).

Strongest single candidate: **C3** — it is the assignment's own hypothesis, it is the only candidate that changes a live decision, and its risks are all bounded by construction: the policy gate still authorizes every action, confidence math is untouched, the fallback makes the zero-data state honest, and the prior path remains byte-identical under regression test until measured evidence earns the override.

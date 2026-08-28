# PulseRecover — Claim Matrix (evidence audit)

**Audit date:** 2026-08-28 · **Auditor:** independent evidence-audit pass (docs-only)
**Scope:** every quantitative or normative claim in `README.md` + `docs/`.
Nothing in this file asserts a number that was not either (a) re-run
first-hand on 2026-08-28, (b) read from a stored run / experiment record /
committed artifact, or (c) explicitly marked **attested** (evidence recorded
elsewhere, not re-run here) or **flagged** (not independently verifiable).

**First-hand re-runs performed for this audit** (this machine, 2026-08-28):

- `cd backend && .venv/Scripts/python -m pytest tests --collect-only -q` →
  **678 tests collected** (suite pass attested by the invariants wave +
  docs/demo.md, same day; collect-only re-verified here).
- Per-directory collects: policy **81**, security **88**, razorpay **47**,
  demo **10**, agenteval **15** (agenteval also executed: **15 passed**).
- `scripts/run_evaluation.py --scenario standard --seed 42 --name
  audit-verify-20260828` → run `run_e1901ece1b754d15867565547d5380b4`,
  dataset `sim_42_50f24b57d0` (the historical canonical dataset id). Every
  published §3 metric of docs/evaluation.md compared **bit-identical**
  (detection, diagnosis, recovery, policy outcomes, holdout, lift, strata).
- `scripts/demo_run.py --scenario all` + `--scenario A` (scratch DBs) → all
  printed numbers in docs/demo.md / README transcripts match **verbatim**.
- Contract check: `contracts/openapi.json` = **29 paths / 29 operations**,
  incl. `POST /api/v1/recovery/opportunities/build` and
  `POST /api/v1/recovery/reconcile`.
- Code/artifact checks: `FEATURE_NAMES` = 58, taxonomy = 8 causes, models =
  21 tables, active pointer `diagnosis_active.json` →
  `diagnosis_random_forest_v20260828T013109Z-77a4ef3b.joblib`
  (9,779,281 bytes, committed; LR rollback 10,170 bytes, committed);
  `sha256(policies/default.yaml)[:12]` = `5a6afe61d6db`;
  12 mutating `/api/v1` routes in the live route table.

**Column legend.** *Reproducible?* — Yes (command given) / Stored (a stored
run row or experiment record holds it) / Attested (recorded evidence exists;
not re-run in this audit) / Flagged. *Current?* — the claim matches the
shipped system today. *Safe to show?* — a skeptical panel can be pointed at
it without qualification beyond what the doc already states.

---

## 1. Test & suite counts

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 1.1 | 678 backend tests | README (setup, repo layout), docs/demo.md, invariants wave | `pytest tests --collect-only -q` → 678 collected; 678 passed 2026-08-28 (invariants-wave full run) | Yes | yes | yes | count updated 645→678 after the invariants wave |
| 1.2 | 81 policy tests | docs/policy.md §6 | collect-only → 81 | Yes | yes | yes | none |
| 1.3 | 88 security tests | docs/security-testing.md | collect-only → 88 | Yes | yes | yes | none |
| 1.4 | 47 razorpay tests | docs/razorpay-integration.md §5 | collect-only → 47 | Yes | **was stale (46)** | yes | corrected 46 → 47 |
| 1.5 | 10 demo proof tests (5 scenarios × 2 runs) | docs/demo.md, README | collect-only → 10; 10/10 ×2 clean runs attested in SHIP_VERDICT | Yes | yes | yes | none |
| 1.6 | agenteval suite green | docs/security-testing.md matrix row 10 | 15 passed (executed this audit) | Yes | **was stale (8/8)** | yes | corrected to 15/15 with date + history note |
| 1.7 | 7 Playwright e2e tests | (lead's submission inventory; not claimed in docs) | 7 `test(...)` in `frontend/e2e/*.spec.ts` | Yes | yes | yes | none (no doc claim) |
| 1.8 | 12 mutating `/api/v1` routes fuzzed for auth | docs/security-testing.md matrix row 1 | live route table via the test's own helper → 12 | Yes | **was stale (13)** | yes | corrected; noted the table is app-derived on every run |
| 1.9 | openapi.json committed & current | README repo layout; decision-log D15 | 29 paths/29 ops; includes build + reconcile routes | Yes | yes | yes | none |
| 1.10 | 21 shared model tables | docs/architecture.md §1 | `len(Base.metadata.tables)` → 21 | Yes | yes | yes | none |

## 2. Demo scenarios A–E (CLI, `scripts/demo_run.py`)

All re-run verbatim this audit (2026-08-28): `scripts/demo_run.py --scenario
all` + `--scenario A` on scratch DBs. Entity ids differ by design (uuid4);
every number below matched.

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 2.1 | A: 2-day dataset, ~144k events (144,625 payment_events) | README, docs/demo.md | re-run: `sim_42_14110f6fcb`, 144,625 events | Yes | yes | yes | none |
| 2.2 | A: SR 82.9% → 69.0% (−16.69%), MEDIUM; 1,359 failed; ₹10,36,582 at risk; GT affected 476 | README, docs/demo.md | re-run transcript identical | Yes | yes | yes | none |
| 2.3 | A: diagnosis `gateway_degradation` conf 0.9740 on `random_forest@v20260828T013109Z-77a4ef3b` | README, docs/demo.md | re-run: 0.9740, same model version | Yes | yes | yes | none |
| 2.4 | A: 2,023 opportunities, ₹15,25,844 in scope | README, docs/demo.md | re-run transcript identical | Yes | yes | yes | none |
| 2.5 | A: ₹8,047 soft-decline → `REQUIRES_APPROVAL` (`approval.amount`), conf 0.8766 over the floor, held by ceiling only | README, docs/demo.md | re-run: 0.8766, REQUIRES_APPROVAL, approve → RECOVERED | Yes | yes | yes | none |
| 2.6 | A: ₹504 + ₹509 timeouts at 0.9545 → `auto_execute.ok` ALLOWED; 3/3 RECOVERED, ₹9,060 total | README, docs/demo.md | re-run transcript identical | Yes | yes | yes | none |
| 2.7 | A tuning note: fail_boost 0.12, `latency_multiplier` 1.0; 0.9740 × 0.98 = 0.9545, × 0.90 = 0.8766 | docs/demo.md | `scripts/demo_run.py:326` config; `app/services/recovery/strategies.py` action-fit table (timeout 0.98, soft_decline 0.90) | Yes | yes | yes | none |
| 2.8 | B: ₹501 card timeout, conf 0.98 ≥ 0.85 → ALLOWED, no human, webhook RECOVERED | README, docs/demo.md | re-run: 0.9800, ALLOWED, RECOVERED ₹501 | Yes | yes | yes | none |
| 2.9 | C: ₹10,143 → `REQUIRES_APPROVAL` (`approval.amount`), `human:ops` approves, executes once, RECOVERED | README, docs/demo.md | re-run transcript identical | Yes | yes | yes | none |
| 2.10 | D: 503 on mutating call → UNKNOWN, no blind retry, re-execute is GET-only (1 mutation total), resolves RECOVERED on gateway evidence | README, docs/demo.md, docs/recovery.md §6 | re-run: "gateway mutations attempted: 1 total"; `TestTimeoutUnknownResolution` asserts one POST ever | Yes | yes | yes | none |
| 2.11 | E: planted refund ₹543 @ 0.99 → BLOCKED (`allowlist`, `never_auto_execute.refund`) → REJECTED, 0 gateway calls, block audited | README, docs/demo.md | re-run: 0 mutations; `TestRefundHasNoExecutionPath` | Yes | yes | yes | none |
| 2.12 | Demo suite timing: 10 passed in 459s / 455s two clean runs (prev. 150s/145s on LR artifact, suite 616) | docs/demo.md | exp07 SHIP_VERDICT run logs | Attested | yes | yes | none |
| 2.13 | Scenario seeds + fixed end date 2026-08-16 → identical numbers every run | docs/demo.md | re-run confirmed; configs in `scripts/demo_run.py:299–380` | Yes | yes | yes | none |

## 3. Live container demo (docs/demo-script.md)

Produced by two rehearsed passes on the compose stack, 2026-08-28 (Appendix
B). Not re-run in this audit (needs the Docker stack); internal consistency
and derivable figures checked.

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 3.1 | Seed 41,354 rows; detection 84.3% → 20.0% (−76.28%), CRITICAL, ₹52,677 at risk, 80 failed | demo-script §1, App. B | Appendix B rehearsal logs (2 passes, identical) | Attested | yes | yes | none |
| 3.2 | Diagnosis `method_outage` 0.9787218468468468 (bit-identical ×2), RF artifact; "top-1 0.910 exact spans / 0.715 prod frames" | demo-script §1 | App. B; matches exp07 MODEL_CARD (0.9098 / 0.7154) | Attested | yes | yes | none |
| 3.3 | 113 opportunities (103 retry + 10 stuck-checkout), ₹73,071 in scope; ₹29,804 recoverable estimate | demo-script §1, App. B | App. B | Attested | yes | yes | none |
| 3.4 | Auto lane ₹100 conf 0.9591 → ALLOWED → RECOVERED; approval lane ₹5,656 → RECOVERED; beat D ₹518 (1 mutation); beat E ₹534 refund BLOCKED; dashboard ₹6,274 recovered / ₹46,921 at risk | demo-script §1, App. B | App. B (both passes identical) | Attested | yes | yes | none |
| 3.5 | Policy version `1.0+sha256.5a6afe61d6db` on audit rows + health | demo-script pre-flight, §4 | `sha256(policies/default.yaml)[:12]` = `5a6afe61d6db` (this audit) | Yes | yes | yes | none |
| 3.6 | Image ships the artifact "~9.8 MB" | demo-script pre-flight | 9,779,281-byte joblib + 666-byte pointer (this audit) | Yes | yes | yes | none (README's "~10 KB" was the stale twin — corrected) |
| 3.7 | Backend image reproduction of test-mode fail-safe: dummy keys → typed `GatewayAuthenticationError`, no 500 | demo-script App. A | attested 2026-08-27 | Attested | yes | yes | none |
| 3.8 | Eval-lab talk track: "today's lift reads null" | demo-script 4:40 beat | **contradicted by first-hand evidence**: the pre-seed curl (defaults → standard/seed 42/0.10 holdout) reproduces canonical → lift **−1.0 pp [−4.6, +2.1]**, rendered as a point+CI, not null (frontend renders `—` only when the point is absent) | Yes | **was wrong** | yes (as corrected) | corrected to the verified reading; "at this small scale" → "the current battery" (the pre-seed is the full preset) |
| 3.9 | "Numbers deterministic within a calendar day" caveat + §6 re-run instruction | demo-script §0 | matches evaluation.md §1 anchor mechanics (verified by audit re-run same-day) | Yes | yes | yes | none |
| 3.10 | e2e ports 8001/3100 "are the e2e suite's" | demo-script pre-flight | `frontend/e2e/stack.ts:10-11` | Yes | yes | yes | none |

## 4. Canonical evaluation — run `run_b371e5b40dc9450a88d052deb03809fe` ("canonical-v2")

**Every figure below was compared bit-for-bit against a fresh same-day
re-run in this audit** (`audit-verify-20260828` /
`run_e1901ece1b754d15867565547d5380b4`, same dataset id
`sim_42_50f24b57d0`, 2026-08-28). All matched exactly. Cross-day
reproducibility is additionally pinned by `ml/experiments/canonical_spec.json`
(`--end-date 2026-08-28`, 3 consecutive runs diffed pairwise — see
docs/evaluation.md §3c).

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 4.1 | Dataset: 68,410 payment_events, 4,897 first-attempt failed rows, 364,686,600 paise, 6 injected incidents, 30 days anchored 2026-08-28 00:00 UTC | README, evaluation.md §3 | audit re-run: 4,897 / 364,686,600 / 6; `sim_42_50f24b57d0` | Yes | yes | yes | none |
| 4.2 | Detection: P 0.333, R 0.667 (4/6), F1 0.444, MTTD 230 min, 116 passes, 12 persisted rows (4 matched / 8 unmatched) | README, evaluation.md §3 | audit re-run: 0.333333 / 0.666667 / 0.444444 / 230.0 / 116 / 12 / 4 | Yes | yes | yes (with the 08-27 reading published alongside) | none |
| 4.3 | Diagnosis on matched windows: top-1 1.000 / top-3 1.000 (4 scored; confidences 0.48 / 0.98 / 0.60 / 0.76) | README, evaluation.md §3 | audit re-run: 4 scored, top1 1.0, top3 1.0, conf 0.4796502975443439 (the disclosed 17th-digit float case) | Yes | yes | yes (small-n caveat in place) | none |
| 4.4 | Recovery: 100 interventions vs baseline 4,897 (98.0% fewer); 94 approvals; 6 ALLOWED outright | README, evaluation.md §3 | audit re-run: interventions 100 (94 retry + 6 link), approvals 94, policy_outcomes {ALLOWED 6, REQUIRES_APPROVAL 94, BLOCKED 1372} | Yes | yes | yes | none |
| 4.5 | Recovered (verified): PulseRecover 2,521,400 paise (28 actions; 28.0% of executed) vs baseline 95,016,400 paise (26.1% of failed amount) | README, evaluation.md §3 | audit re-run: 2,521,400 / 28; baseline 95,016,400 / 0.260543 | Yes | yes | yes (gross labeled; honest-read paragraph in place) | none |
| 4.6 | False interventions 421 vs 6; unsafe 0 vs 4,897 ungated; UNKNOWN 0 | README, evaluation.md §3 | audit re-run: 421 / 6 / 0 / 0 | Yes | yes | yes | none |
| 4.7 | 1,472 opportunities = 1,116 failed-payment + 356 stuck-checkout (₹254,080); all 356 stuck proposals BLOCKED (203 incident-cap / 181 global brake / 9 opt-out / 2 duplicate) | README, evaluation.md §3 | totals verified (1,472 built; 1,372 BLOCKED); **the 1,116/356 split and per-rule counts are not in the stored metrics JSON** — derived during run analysis; internally consistent (1,116+356 = 1,472) | Stored (totals) / Attested (split) | yes | yes (split traceable to run analysis narrative) | flagged in §16 |
| 4.8 | Rate brakes hit exactly: 10/incident, 100/hour global (100 executed hit it exactly); wall-clock vs sim-time artifact disclosed | evaluation.md §1/§3 | audit re-run consistent (100 executed, 1372 blocked) | Yes | yes | yes | none |
| 4.9 | Holdout: 170/1,847 customers held out (realized 9.2% @ configured 10%); treatment 4,424 failed / 610 recovered (13.79%; 28 action + 582 organic); holdout 473 / 70 (14.80%) | README, evaluation.md §2/§3 | audit re-run: 1677/170, 4424/610/0.137884, 473/70/0.147992 | Yes | yes | yes | none |
| 4.10 | Raw ITT lift −1.0 pp [−4.6, +2.1] (Newcombe); class-adjusted +0.1 pp [−2.9, +3.2] | README, evaluation.md §3 | audit re-run: −0.010108 [−0.046305, 0.020878]; adjusted 0.001445 [−0.02931, 0.032201] | Yes | yes | yes (presented with CI + power analysis, not as a headline) | none |
| 4.11 | Per-class strata (soft_decline +1.0, insufficient_funds +3.9, timeout −4.3, abandonment −1.4, hard_decline −0.5 pp) and per-method (upi −4.2, card +5.2, netbanking −1.8, wallet −4.5) | evaluation.md §3 | audit re-run: all 9 strata match to rounding | Yes | yes | yes | none |
| 4.12 | Median TTR 4,874 vs 5,157 min; attribution window ≤ 698.1 h; isolation 0 opportunities / 0 actions for holdout | evaluation.md §3 | audit re-run: 4873.74 / 5156.71 / 698.1 / 0 / 0 | Yes | yes | yes | none |
| 4.13 | Executed actions convert 28.0% vs ≈13.3% organic (652/4,897); expected ITT effect ≈ +0.3 pp vs ±1.7 pp noise (MDE ≈ 5 pp) — Lewis & Rao power problem | evaluation.md §3 | 28/100 = 28.0% (audit re-run); organic 652/4,897 = 13.31% | Yes | yes | yes | none |
| 4.14 | Same-day bit-reproducibility (two disclosed exceptions: wall-clock MTTR; 17th-digit float in one confidence) | evaluation.md §1/§3 | confirmed by this audit's re-run; also `canonical-v2-repro` `run_5d22f898…` | Yes | yes | yes | none |

## 5. Evaluation history & provenance (evaluation.md §3b)

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 5.1 | `run_caa1f1a9…` ("final", 08-27): P 0.667 / R 0.500 / F1 0.571, MTTD 895, diagnosis 0.667 (3), 655/60, 1,380,700 paise, lift −3.8 pp [−7.7, −0.4] | evaluation.md §3b | stored run; ml/experiments/detection/index.md exp000 | Stored | yes (as history, both anchors stated) | yes | none |
| 5.2 | `run_0022000d…` ("after-new-signals", 08-27): P 0.778 / R 1.000 / F1 0.875, MTTD 585, diagnosis 0.667 (6), 903/90, 2,452,900 paise, lift −3.7 pp | evaluation.md §3b | stored run; detection/index.md final row | Stored | yes | yes | none |
| 5.3 | v1→v2 delta attribution (three recall signals; zero new quiet-control FPs; +77.7% recovered revenue on the wider net; ₹173,659 newly surfaced) | evaluation.md §3b, detection.md | runs `run_4f3b346e…` / `run_0022000d…`; exp001–003 records | Stored | yes | yes | none |
| 5.4 | Historical correction: stale 1,945,400-paise table corrected to 1,380,700; 0.156 precision on 90 rows pre-redesign | evaluation.md §3b provenance | documented correction (kept visible, not erased) | Stored | yes | yes | none |
| 5.5 | Window-sensitivity statement: same v2 engine, 6/6 @ P 0.778 (08-27) vs 4/6 @ P 0.333 (08-28), no code change between runs | README, evaluation.md §3/§4, detection.md | both runs stored; detection/simulator code byte-identical across them | Stored | yes | yes | none |
| 5.6 | detection.md floors before/after pair (90→6 rows, P 0.156→0.667) | detection.md "Measured effect" | replay measurements, 2026-08-27 | Attested | yes | yes | downstream-row provenance annotated (719/60/6 predates the holdout arm; number of record = 655/60/5 in §3b) |
| 5.7 | Stale cross-ref: "the published 0.185/0.833 in docs/evaluation.md … anchored 2026-08-26" | detection.md ⚠️ note | 0.185/0.833 no longer appears in evaluation.md (superseded by §3b history) | — | **was stale** | yes | corrected to describe the actual anchors (pre-fix 08-26, pair 08-27, current runs 08-27/08-28) |

## 6. ML diagnosis (docs/ml.md, ml/experiments/diagnosis/)

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 6.1 | 58 features, 8-class taxonomy, temporal 60/20/20 no-shuffle split, window-local features | ml.md §1–3 | `FEATURE_NAMES` = 58, `CAUSES` = 8 (this audit) | Yes | yes | yes | none |
| 6.2 | §5 preliminary synthetic: 1,600 windows (200/class), split 960/320/320, test macro-F1 0.9938 for all three algos; labeled PRELIMINARY/SYNTHETIC, upper bound | ml.md §5 | run record `v20260826T182714Z-d54d647f` | Attested | yes (label intact) | yes | none |
| 6.3 | Heuristic fallback 91.5% top-1 (439/480, 60/class), confidences ≤ 0.7, flagged `heuristic=true` | README, ml.md §5 | ml.md record; cap in `app/services/diagnosis/` | Attested | yes | yes | none |
| 6.4 | §8 exact-span dataset: 2,050 windows, 60 seeds, 1,150 positives + 900 no_fault, split 1230/410/410 | ml.md §8 | `backend/artifacts/sim_features.csv` + README "Regenerating" commands | Attested (regen path documented) | yes | yes | regeneration section corrected (it reproduces the §8 LR, not the active RF) |
| 6.5 | §8 model table: LR 0.8444/0.8231/0.8780/0.9927; RF 0.8061/0.7962/0.9171/1.0000; GB 0.8363/0.8320/0.9268/0.9927 (val F1/test F1/top-1/top-3) | README, ml.md §8 | training run `v20260826T234303Z-c5434878`; exp06 continuity table re-measured identical | Attested | yes | yes | none |
| 6.6 | §8 per-class table labeled "the active pointer" | ml.md §8 | pointer is now the exp07 RF | — | **was stale** | yes | caption corrected (active until exp07; kept for rollback) |
| 6.7 | §8 post-training checks: fresh seed 777 → 6/6 top-1 on exact spans; diluted-window reading 0.60/0.80 "in evaluation.md §2" | ml.md §8 | 0.60/0.80 is an old reading; section ref wrong | — | **was stale** | yes | dated + pointed to current §3/§3b readings (1.000 on 4 / 0.667 on 6) |
| 6.8 | §9 prod-frame v2: 506 rows, 144 seeds, split 303/101/102; old artifact unsafe 0.528 / false-fire 0.181 / safe 0.0055 — NO-SHIP, rollback same day | ml.md §9 | exp01–exp06 records; exp05 baselines/metrics.json | Attested | yes | yes | none |
| 6.9 | §10 exp07 gate: prod safe 0.2708 vs 0.1906; unsafe 0.0928 vs 0.567; F1 0.6293 vs 0.4991; span top-1 0.9098 vs 0.8780; demo A 0.974 / D 1.000; 10/10 demo ×2; 645 passed | ml.md §10 | `metrics_v4b2.json` (incumbent block matches exactly), SHIP_VERDICT.md | Yes (records read; incumbent numbers spot-verified) | yes | yes (tradeoffs disclosed in same section) | none |
| 6.10 | Active pointer = `random_forest v20260828T013109Z-77a4ef3b`; LR kept for rollback | README, ml.md §10, demo docs | `backend/artifacts/diagnosis_active.json` (this audit) | Yes | yes | yes | none |
| 6.11 | Disclosed tradeoffs: span macro-F1 −0.057 (bank_downtime 0.350→0.000 @ 15), auto_coverage 0.758→0.364, span ECE 0.051→0.147; stricter continuity clause failed (0.7664 < 0.7931) and disclosed | ml.md §10, SHIP_VERDICT | `metrics_v4b2.json`, `config_v4b2.json` | Yes | yes | yes | none |
| 6.12 | exp07 live check: seed 777 → 6/7 top-1 via HTTP stack, miss = hedged no_fault 0.63 | ml.md §10, SHIP_VERDICT | `ml/experiments/diagnosis/live_check.log` (read: 7 incidents, 6 correct, no_fault 0.63 + one 0.43) | Yes | yes | yes | none |
| 6.13 | Demo-script talk track: "held-out top-1 0.910 exact spans / 0.715 prod 12h frames" | demo-script §1 | MODEL_CARD: 0.9098 / 0.7154 | Yes | yes | yes | none |

## 7. Agent evaluation (docs/agent.md, ml/experiments/agent/)

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 7.1 | Corpus: 36 cases, agent-corpus-1.0; exp01 vs exp02 table (policy_compliance 0.4917 → 1.0000, unsafe_recommendation_rate 0.7500 → 1.0000, etc.) | agent.md | `exp01_baseline/metrics.json` + `exp02_confidence_safety/metrics.json` — **read and matched cell-for-cell** | Yes | yes | yes | none |
| 7.2 | Adversarial matrix outcomes (10 rows), zero gateway mutations in every case | agent.md | exp02 metrics: `gateway_mutations: 0`; suite executes green (15 passed) | Yes | yes | yes | none |
| 7.3 | LLM loop bounds: max 6 iterations, max 2 attempts; two rogue tool calls abort | agent.md | `reasoners.py:620-631,753-754` (this audit) | Yes | yes | yes | none |
| 7.4 | Mutation path = exactly 2 tools; amounts copied from original rows; never calls the gateway | agent.md, README | `AgentTools` whitelist (9 tools) + architecture boundary test | Yes | yes | yes | none |
| 7.5 | Scripted (not live-model) LLM eval path — limits stated | agent.md "Remaining weaknesses" | doc itself | — | yes | yes | none |

## 8. Detection engine (docs/detection.md)

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 8.1 | Four metrics + grids (5/5/30/60 min), count floors, per-metric floor defaults | detection.md | `app/services/detection/` params | Yes | yes | yes | none |
| 8.2 | Floors+dedup effect: 90 → 6 rows, P 0.156 → 0.667, R 0.500, MTTD 415 → 895 | detection.md | replay pair 2026-08-27; exp000 baseline consistent | Attested | yes | yes | downstream row annotated (see 5.6) |
| 8.3 | Recall attack: 6 → 9 rows, P 0.778, R 1.000, F1 0.875, MTTD 585, 6/6 kinds; zero new quiet-control FPs | detection.md, ml/experiments/detection/index.md | runs `run_4f3b346e…` / `run_0022000d…`; exp001–003 | Stored | yes | yes | none |
| 8.4 | Detector comparison fixture table (zscore 0.989/0.978, ewma …, IF 0.923/0.400), labeled synthetic-fixture | detection.md | `tests/detection/test_comparison.py` | Attested (suite green) | yes | yes (label intact) | none |
| 8.5 | Known-limitations list (baseline poisoning, sparse traffic, payday-scale boundary recall 0/1, route-scan organic admit, seasonality) | detection.md | exp003 failure_analysis.md; payday replay | Attested | yes | yes | none |

## 9. Policy & safety (docs/policy.md, README, ADR 0003, policies/default.yaml)

Config re-read and hashed this audit: `sha256[:12] = 5a6afe61d6db` →
`policy_version 1.0+sha256.5a6afe61d6db`. Every value below matched the file.

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 9.1 | Auto lane: confidence ≥ 0.85 AND amount ≤ ₹5,000 AND attempts < 2 | README, policy.md, ADR 0003 | `policies/default.yaml` (0.85 / 5000 / 2) + rule R12 | Yes | yes | yes | none |
| 9.2 | security-architecture.md said "attempts ≤ 2" | security-architecture.md §4 | code: R12 fires at attempts_so_far ≥ 2 → auto means < 2 | Yes | **was wrong** | yes | corrected to "< 2 (i.e. first or second attempt)" |
| 9.3 | Hard blocks: `refund` absent from allowlist + in `never_auto_execute` (no approval lane ever); irreversible + opted-out hard-blocked | README, policy.md | yaml: allowlist (8 types, no refund), never_auto_execute [refund, irreversible_action, customer_opted_out] | Yes | yes | yes | none |
| 9.4 | Rate limits: 10/incident, 3/customer/day, 100/global hour; duplicate cooldown 60 min; stopping rules 3+3 | README, policy.md | yaml values matched | Yes | yes | yes | none |
| 9.5 | Fail-closed: malformed (NaN, non-INR, negative) → BLOCKED; strict loader refuses unknown keys; kill switch exempts only non-financial | README, policy.md | yaml + policy tests (81) | Yes | yes | yes | none |
| 9.6 | Every decision persisted immutably; BLOCKED mirrored to append-only audit | policy.md §4 | code + tests | Yes | yes | yes | none |
| 9.7 | "0 unsafe actions" (asserted in suite) | README, evaluation.md §1 | audit re-run: `unsafe_action_count = 0`; suite invariant tests | Yes | yes | yes | none |
| 9.8 | Monotone-conservative stricter-of-two bounds; BLOCKED > REQUIRES_APPROVAL > ALLOWED | policy.md §1 | engine code | Yes | yes | yes | none |

## 10. Architecture, idempotency, webhooks (docs/architecture.md, data-flow.md, razorpay-integration.md, recovery.md)

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 10.1 | Modular monolith; ports-only coupling; dependency matrix enforced by AST test | architecture.md §1/§4, ADR 0010 | `tests/architecture/test_boundaries.py` (in the 678) | Yes | yes | yes | none |
| 10.2 | Agent can never reach the gateway; policy engine depends on nothing probabilistic | architecture.md §4 | boundary test rules | Yes | yes | yes | none |
| 10.3 | One mutation per action ever; `gateway_request_id` UNIQUE → order `receipt` / link `reference_id`; timeout/5xx → UNKNOWN → GET-only resolve | README, recovery.md §5, razorpay-integration.md §3 | executor code + failure-mode tests (incl. one-POST wire assertion) | Yes | yes | yes | none |
| 10.4 | Webhooks: raw-body HMAC-SHA256, constant-time, fail-closed; `x-razorpay-event-id` UNIQUE dedup → 200 `already_processed` zero side effects; out-of-order safe (captured wins) | README, razorpay-integration.md §4 | `app/api/v1/webhooks.py` (read), webhook tests (47-dir) | Yes | yes | yes | none |
| 10.5 | `SIMULATION_MODE=true` or missing keys → twin always; app can never silently hit the network; health reports the real mode | README, razorpay-integration.md §1 | `factory.py` predicate + health endpoint | Yes | yes | yes | none |
| 10.6 | Backoff only on idempotent GETs (0.25s·2ⁿ, max 3) | razorpay-integration.md §3 | client code + `TestTimeoutsBounded` (3 GET / 1 POST) | Yes | yes | yes | none |
| 10.7 | Reconcile sweep: operator-triggered, GET-only + same handler registry; no background scheduler | ADR 0011, data-flow.md §8 | `POST /api/v1/recovery/reconcile` present in contract | Yes | yes | yes | none |
| 10.8 | Money = integer paise everywhere; thresholds INR→paise at load; exactly ₹5,000.00 within ceiling | architecture.md §6, policy.md §3 | config loader + money tests | Yes | yes | yes | none |
| 10.9 | Strategy confidence = diagnosis × action-fit; 0.80 evidence default when no diagnosis (under the 0.85 floor by construction) | recovery.md §3 | `strategies.py`: `DIAGNOSIS_FREE_EVIDENCE = 0.80` (this audit) | Yes | yes | yes | none |
| 10.10 | Stuck-checkout source: `created` ≥ 30 min at build time → `stuck_checkout_payment`, link-first | recovery.md §2, detection.md | `STUCK_CREATED_THRESHOLD = 30 min` (this audit); `test_stuck_checkout.py` | Yes | yes | yes | none |

## 11. Razorpay integration & honesty labels

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 11.1 | All Razorpay API facts sourced to public docs, fetched 2026-08-26, re-verified 2026-08-27 (URLs inline; UNVERIFIED items marked) | research.md | research.md itself | Attested (fetch dates recorded) | yes | yes | none |
| 11.2 | Simulator described precisely as modeled on documented API semantics + test-mode behaviors; no proprietary infrastructure/routing/issuer/network telemetry | README, simulator.md, razorpay-integration.md | research.md is public-docs-only | — | yes | yes | **added the precise descriptor to all three surfaces** (simulator.md "Scope of realism", README Razorpay section, razorpay-integration.md §1) |
| 11.3 | Real vs simulated separated wherever integration is discussed (SIMULATION ONLY labels; health reports mode; test-mode setup separate) | razorpay-integration.md, demo-script App. A, README | doc structure + factory predicate | Yes | yes | yes | none |
| 11.4 | Test-mode facts used in demo guidance: test cards per error_reason, UPI handles, 30-link cap, no UPI links, 3-day tokens, key-prefix mode selection | razorpay-integration.md §2, demo-script App. A | research.md (verified 2026-08-27, raw HTML) | Attested | yes | yes | none |
| 11.5 | "Razorpay has no retry-a-payment API; fresh order IS the retry primitive" | recovery.md §8 | research.md Payments section ("read-mostly; capture only") | Attested | yes | yes | none |
| 11.6 | Differentiation table (Smart Retries fixed T+1/2/3 not configurable — classic stack; FPR one-time; IPR checkout-only; Optimizer opaque; Agent Studio announced) | README, research.md, competitive-analysis.md | research refresh: "not configurable" true of classic Subscriptions stack only; docs carry the qualifier where required (competitive-analysis §7) | Attested | yes | yes | none |
| 11.7 | Network resubmission guidance "~15 per 30 days" | README problem section, policy.md T-assets | PSP secondary sources; Visa/Mastercard primary UNVERIFIED (research.md open questions); README phrases it as guidance with "~" | Attested | yes | yes (as "guidance", not network law) | none |
| 11.8 | Insights "platform callout" compares against the **simulated** fleet only | data-flow.md §10, product-strategy.md §4.2 | `app/services/insights/service.py` strings say "simulated fleet"; scope field `simulated_fleet` | Yes | yes | yes | D16 status note records the label |

## 12. Simulator (docs/simulator.md)

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 12.1 | Determinism: one seeded RNG, fixed consumption order, deterministic ids, run id `sim_{seed}_{hash[:10]}`; force/delete semantics | simulator.md | determinism tests; audit re-run reused the historical dataset id `sim_42_50f24b57d0` | Yes | yes | yes | none |
| 12.2 | Verified performance: ~6.3–6.5 s (~21k rows/s) → 67,727 events / 30,491 payments / 3,000 customers / 150 subs / 469 GT rows / 131,602 total; events_per_payment 2.22 | simulator.md | dated measurement (2026-08-27), labeled with machine/config | Attested | yes (day-anchored, dated) | yes | none |
| 12.3 | Distributions are synthetic choices, not measured Razorpay statistics | simulator.md | doc itself; reinforced by new "Scope of realism" note | — | yes | yes | strengthened (11.2) |
| 12.4 | 6 incident kinds + default schedule fractions; affected-count semantics | simulator.md | `app/simulator/config.py` | Yes | yes | yes | none |
| 12.5 | Day-anchor mechanics: unset `end_date` → today 00:00 UTC; same-day bit-reproducibility; `--end-date` pin now ships (canonical spec) | simulator.md, evaluation.md §1/§3c/§4 | engine code; audit re-run same-day identical; canonical_spec.json 3× pairwise-diff proof | Yes | yes | yes | none |

## 13. Revenue methodology (docs/revenue-methodology.md)

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 13.1 | Four numbers kept distinct; `actual_recovered` = verified RECOVERED ledger only, UNKNOWN excluded | revenue-methodology.md §1/§8 | engine + tests; evaluation metric definitions | Yes | yes | yes | none |
| 13.2 | Recoverability/effectiveness factors are documented priors, vendor-claim-anchored (Stripe ~55% named as vendor claim); tests pin ordering | revenue-methodology.md §4/§5/§9 | `RevenueConfig`; `test_config_monotonicity.py` | Yes | yes | yes | none |
| 13.3 | Wilson interval math + conservative endpoint summation | revenue-methodology.md §6 | `statistics.py` | Yes | yes | yes | none |
| 13.4 | Worked example numbers | revenue-methodology.md §7 | synthetic test population (labeled) | Attested | yes | yes | none |

## 14. Security testing (docs/security-testing.md)

| # | Claim | Where stated | Source | Reproducible? | Current? | Safe? | Action taken |
|---|---|---|---|---|---|---|---|
| 14.1 | 15 attack vectors + secret sweep, 88 tests, every fix has a regression test | security-testing.md | collect-only → 88; matrix rows 1–15 + S | Yes | yes | yes | intro count corrected 13 → 15 |
| 14.2 | VULN-1..7 found and fixed with named regression tests | security-testing.md | tests exist in `tests/security/` (suite green) | Yes | yes | yes | none |
| 14.3 | Accepted risks (well-known sim webhook secret; demo-grade auth; in-memory rate limits; SQLite single-writer) | security-testing.md, security-architecture.md | docs + code | Yes | yes | yes | none |
| 14.4 | Mandatory guarantees table (no secret leakage, no unrestricted LLM execution, no duplicate financial action, …) | security-testing.md | named proof tests | Yes | yes | yes | none |

## 15. Corrections applied in this audit (grouped)

**Wrong (fixed):**
1. README Deployment: artifact size "~10 KB total" → "~9.8 MB total"
   (RF joblib is 9,779,281 bytes; ~10 KB described the old LR artifact).
2. README "Regenerating the ML artifacts": claimed a fresh clone has no
   trained model and that the two-command flow is "exactly how the shipped
   artifact was produced" — both false since the exp07 RF ship. Rewritten:
   active pair + rollback are committed; the flow reproduces the §8 LR
   (rollback); the RF pipeline is exp07's records. Also "allowlist exactly
   those two files" → three files (pointer + active + rollback joblibs).
3. docs/security-architecture.md §4: "attempts ≤ 2" → "attempts < 2".
4. docs/demo-script.md 4:40 beat: "today's lift reads null" → verified
   reading "−1.0 pp, 95% CI [−4.6, +2.1], brackets zero"; dropped the wrong
   "small scale" framing (the pre-seed is the full preset).

**Stale (fixed):**
5. docs/razorpay-integration.md §5: 46 → 47 tests.
6. docs/security-testing.md: 13 → 15 attack vectors; "8/8 agenteval" →
   "15/15 (verified 2026-08-28; 8/8 at the time)"; "all 13 mutating routes"
   → "all mutating routes in the live table (12 at last verification)".
7. docs/index.md: 13-vector → 15-vector; claim-matrix row added.
8. docs/ml.md §8: per-class caption no longer calls the LR "the active
   pointer"; §8's "top-1 0.60/top-3 0.80 in evaluation.md §2" dated and
   pointed at current §3/§3b readings.
9. docs/detection.md: stale "published 0.185/0.833 in docs/evaluation.md"
   cross-ref rewritten to the real anchors; floors-table downstream row
   (719/60/6) annotated as pre-holdout-arm, number of record identified.
10. docs/decision-log.md: ADR range 0001–0009 → 0001–0011; statuses D14/D16/
    D18 "Roadmap P1" → Shipped; D15 "In progress" → Closed.
11. docs/product-strategy.md: §7 detection "current: 0.185/0.833" date-
    stamped (pre-fix, fix shipped); §6 diagnosis 0.878/0.993 attributed to
    the §8 LR with the exp07 RF reading alongside; §9 demo-story numbers
    marked indicative-at-writing.
12. README/evaluation.md: "pre-registered exp06 gate" → "pre-registered
    campaign gate (exp06 clauses, applied in exp07)" with frame names
    (exact-span top-1; prod-frame unsafe-side error).

**Razorpay-honesty additions:**
13. simulator.md "Scope of realism", README Razorpay section,
    razorpay-integration.md §1: the twin is "modeled on documented Razorpay
    API semantics + test-mode behaviors … no proprietary Razorpay
    infrastructure, routing, issuer, or network telemetry".

**Metric-discipline additions:**
14. README results section: compact metric card (run id, scenario, seed,
    anchor, denominators, definitions, gross vs incremental, simulator-vs-
    Test-Mode basis, canonical vs history).
15. README header: link to this matrix.

**Ban-list check (no action needed):** no "up to X%" for our own numbers
(only explicitly labeled vendor claims); no ROI claims; no "AI accuracy"
vagueness (every model number carries frame + denominator); no cherry-picked
runs (all published runs are in §3/§3b with both anchors); no production
claims (demo-grade auth and simulator basis stated in README + every
results doc).

## 16. Left flagged / not independently verifiable in this audit

| # | Item | Status | Owner follow-up |
|---|---|---|---|
| 16.1 | Stuck-checkout split (1,116/356), ₹254,080, per-rule block counts (203/181/9/2) for canonical-v2 | Totals verified bit-identical (1,472 built / 1,372 BLOCKED / 100 executed); the split itself was derived during run analysis and is not in the stored metrics JSON. Internally consistent; not contradicted anywhere. | Optional: persist an `opportunity_types` breakdown in the run metrics so the split is machine-checkable. |
| 16.2 | Live container demo numbers (§3 of this matrix) | Attested by two rehearsed passes (Appendix B), not re-run here (needs Docker). Internal consistency checked; derivable items (policy hash, artifact size, ports) verified. | Re-run §6 pass on demo morning per the runbook's own instruction. |
| 16.3 | Router module docstring: "default scale (10 days / 12k events per arm)" | Stale — a bare `POST /api/v1/evaluation/run` defaults to scenario `standard` at full preset (30d/65k). Code comment, not docs-owned. | Lead/code owner: correct the docstring. |
| 16.4 | `.gitignore` / `.dockerignore` comments still say "~10 KB" for the active model | Cosmetic staleness in repo-root config comments (not docs-owned). README + demo-script corrected. | Lead/code owner: update the comments. |
| 16.5 | Wall-clock figures ("~1.5 min" canonical, "~55 s" pre-seed, "~2 min" demo-all, 459s/455s demo suite) | Machine-dependent approximations; this audit's canonical re-run took ~64 s. All are phrased as approximations and none is load-bearing. | none |
| 16.6 | Razorpay doc facts (test cards, caps, enums, webhook behavior) | Attested to research.md fetch dates (2026-08-26/27); not re-fetched in this audit. Vendor pages can change; UNVERIFIED items are already marked in research.md. | none |
| 16.7 | exp01–exp06 intermediate ML numbers (tables in ml.md §9) | Read from experiment records and found internally consistent; not recomputed from raw CSVs. The ship-relevant blocks (incumbent gate numbers) were spot-verified against `metrics_v4b2.json`. | none |
| 16.8 | demo-script beat E uses ₹534 while CLI scenario E uses ₹543 | Not an error: different surfaces (live stack vs CLI demo), each internally consistent and verified on its own surface. Noted so a panelist comparing them is not surprised. | none |

## 17. Cross-doc consistency statement

After the corrections in §15, the following previously disagreeing pairs now
agree: README vs demo-script artifact size; ml.md §8 vs §10 active-pointer
statements; detection.md vs evaluation.md published detection numbers;
security-testing.md vs actual suite counts; decision-log statuses vs shipped
state; product-strategy "current" metrics vs published metrics. No two docs
now assert different values for the same quantity without naming the frame
(anchor day, artifact, or harness version) that distinguishes them.

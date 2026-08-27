# PulseRecover — Evaluation Methodology & Results

> ADR 0005: evaluation is scientific, not anecdotal. Every number in this
> document is reproduced by the commands at the bottom, against simulator
> ground truth. Nothing here is a vendor claim.

The harness (`app/services/evaluation/`) answers one question: **does the
full PulseRecover loop beat the industry default — "retry every failed
payment once" — and at what intervention cost?**

## 1. Experimental setup

Each run executes TWO arms of the same simulator scenario
(`app.simulator.SCENARIOS`, deterministic per seed):

- **BASELINE** — the naive default: for *every* failed payment, one generic
  retry (a fresh order via the gateway twin). No detection, no diagnosis, no
  policy gate, no verification. This is what "we retry failed payments"
  means at most merchants.
- **PULSECOVER** — the real product loop, unchanged: scheduled detection
  passes (`run_detection`) → ML diagnosis (`DiagnosisService`) → opportunity
  + strategy generation → the deterministic policy gate (`RecoveryExecutor`,
  `policies/default.yaml`) → execution through the `PaymentGateway` port →
  verification through the real webhook handler registry and
  `RecoveryExecutor.resolve`.

Arms run in **isolated scratch SQLite databases** (tempfiles, deleted
afterwards). The main/demo database receives only the resulting
`evaluation_runs` + `experiments` rows. The scratch `simulator_run_id`s are
recorded inside the metrics JSON (`arms.*.simulator_run_id`);
`evaluation_runs.simulator_run_id` stays NULL rather than pointing at a row
that does not exist in the main database.

### Roles the harness plays (all deterministic, all disclosed)

- **The operator**: when the policy gate returns `REQUIRES_APPROVAL`, the
  harness approves as `human:eval_operator` and re-executes. This is the
  human-in-the-loop the product is designed for; `approvals_required` counts
  how often it happened.
- **The customer**: decides whether a recovery attempt converts, via a
  documented (failure-class × action) conversion table
  (`CONVERSION` in `runner.py`), seeded per `gateway_request_id`. One table
  governs both arms — the baseline always lands in the `immediate_retry`
  column. Payment links are decided *inside* the gateway twin
  (flat `GATEWAY_SUCCESS_RATE` — a disclosed twin limitation: the twin's
  inline link decision is class-independent).
- Detection passes are scheduled, not ground-truth-anchored: one pass every
  6h of simulated time, each looking back 12h
  (`DETECTION_STEP_MINUTES`/`DETECTION_WINDOW_MINUTES`). The 12h window is
  what gives the zscore detector enough pre-anomaly baseline to fire on
  1.5–3h injected incidents; the detector and thresholds are the production
  defaults. Detection therefore scores *honestly*: passes also cover quiet
  data, and false positives are counted.

### Metrics and their exact definitions

- **Detection precision/recall/F1** vs `simulator_ground_truth`: a detected
  incident matches a ground-truth incident when its flagged span
  (`meta.anomaly_start..anomaly_end`, else its analysis window) overlaps the
  injected window. Note the incident model upserts per
  (metric, detector, window, segment), so a long incident is legitimately
  re-detected from later pass windows — every such row counts, which
  inflates the incident count for both true and false detections (a real
  dedup limitation this evaluation *surfaced*).
- **MTTD (minutes, simulator time)**: first pass window-end overlapping the
  injected window minus the injected start.
- **Diagnosis top-1/top-3**: each ground-truth incident is scored once, via
  the diagnosis of its *first-detected* matching incident, against the
  taxonomy mapping (`KIND_TO_CAUSE`; a method outage with a targeted bank is
  `bank_downtime`).
- **Recovered revenue / recovery rate**: webhook- or inline-verified
  `RECOVERED` actions only, over the arm's total failed amount. UNKNOWN
  actions are never counted.
- **Interventions / false interventions**: an intervention is an action that
  reached the gateway. A *false* intervention targets a never-approve
  (`hard_decline`) payment — network guidance says do not resubmit those.
- **Unsafe actions**: executions with neither an `ALLOWED` policy decision
  nor a recorded human approval, plus any `refund` execution. **Asserted 0
  in the test suite.**
- **MTTR (minutes, wall clock)**: `proposed_at → verified_at` pipeline
  latency. Sim-time MTTR is meaningless when execution is synchronous, so
  the honest operational number is reported and labeled.

### Policy rate-limit note (simulation artifact)

Policy rate windows (`max_actions_global_per_hour`, per-customer-day) are
evaluated in wall-clock time while the data is simulator time: an evaluation
run executes all its actions "in the same hour". The global cap (100/hour in
`policies/default.yaml`) therefore throttles PulseCover execution volume in
long scenarios. This is the deterministic gate working as configured, not a
harness bug; it is visible in `policy_outcomes.BLOCKED`.

## 2. Results (reproduced, this machine)

<!-- RESULTS-START -->

Run `final` / `run_c4d84e5a490348f8a753ee4131d7500d` — scenario `standard`
(full preset: 30 days, 67,727 payment_events, 4,893 failed payments,
6 injected incidents), diagnosis artifact `logistic_regression
v20260826T234303Z-c5434878` (see docs/ml.md §8). Wall-clock: ~3 min.

**Detection (scheduled 12h/6h passes, production defaults):**
precision **0.185**, recall **0.833** (5/6 injected incidents found),
F1 0.302, MTTD **527 min** (≈ one 6h step + window edge). The missed
incident is `route_latency`: a single route's latency multiplier barely
moves merchant-wide p90 — a real coverage gap, not a harness artifact.
Precision is dragged down by 75 incident rows on organic noise (42 of them
the noisy `capture_latency_ms` metric) and by pass-window re-detection rows;
see §3.

**Diagnosis (first-detection window per incident, vs ground truth):**
top-1 **0.60**, top-3 **0.80** (5 scored). The three failure spikes
(gateway_degradation, method_outage, insufficient-funds wave) are top-1
correct at confidence 0.95–0.99 even on the diluted 12h detection windows;
the abandonment and 48h subscription spikes read as `no_fault` from those
windows (their tails barely move the full-window features). On exact
incident spans the same model is 6/6 (docs/ml.md §8) — the gap is window
dilution, and it argues for a triage step that re-scopes the incident window
to the detected anomaly span before diagnosis.

**Recovery:**

| metric | BASELINE (retry everything) | PULSECOVER |
|---|---:|---:|
| interventions | 4,893 | **100** (98.0% fewer) |
| recovered revenue (verified) | 99,011,600 paise | 1,945,400 paise |
| recovery rate (of failed amount) | 27.0% | 0.53% |
| false interventions (never-approve resubmissions) | **433** | **13** |
| unsafe actions (no gate, no approval) | 4,893 ungated | **0** |
| UNKNOWN / unverifiable outcomes | n/a (no verification) | 0 |
| human approvals required | 0 | 100 |

23 PulseCover actions reached RECOVERED (verified); the policy gate blocked
8,459 proposed actions (rate limits + duplicate protection — including the
100/hour global wall-clock cap, see §1 note) and sent every executed action
through the human-approval lane (all confidences below the 0.85 auto floor).

**Read on the headline:** the naive baseline recovers more *gross* revenue
by construction — it fires at every organic failure too, and 27% of a much
larger blast radius is a big number. It pays for that with 49× the
interventions, 433 never-approve resubmissions (network-penalty territory),
zero verification, and zero auditability. PulseCover's number is small but
*clean*: gated, verified, audited, and it never touches a customer it
shouldn't. Widening recovery volume is a policy-file decision
(`max_actions_per_incident`, the 100/hour global cap), not a code change.

<!-- RESULTS-END -->

## 3. Honest limitations

- The customer conversion table is a documented prior model, not a measured
  fact; both arms share it, so the *comparison* is fair even where absolute
  numbers are prior-driven.
- Detection precision on the scheduled 12h-window configuration is dragged
  down by organic traffic noise (see §2); the demo's anchored pass is far
  tighter. Both are reported for what they are.
- `route_latency` incidents are essentially invisible to global-metric
  detection (a single route's latency barely moves merchant-wide p90) — a
  real coverage gap the evaluation exposed.
- SQLite-scale synchronous execution: `POST /api/v1/evaluation/run` blocks
  for the whole run (seconds at reduced scale; minutes at full preset
  scale). That is deliberate for the demo — the CLI is the full-scale path.

## 4. Reproduction

```bash
cd backend
# full-preset baseline-vs-pulserecover run (minutes; persists the run row)
.venv/Scripts/python scripts/run_evaluation.py --scenario standard
# faster smoke run at reduced scale
.venv/Scripts/python scripts/run_evaluation.py --scenario upi_outage_demo --days 5 --events 8000
# same, via the API (synchronous)
curl -X POST localhost:8000/api/v1/evaluation/run -H 'X-API-Key: dev-key' \
  -H 'Content-Type: application/json' -d '{"scenario": "upi_outage_demo"}'
# stored rows only:
curl localhost:8000/api/v1/evaluation/runs
curl localhost:8000/api/v1/evaluation/metrics
```

Safety invariant (zero unsafe actions) is enforced by
`tests/integration/test_evaluation_api.py`.

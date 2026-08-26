# ADR 0005: Simulator with ground truth for scientific evaluation

- **Decision:** A simulator implements the same `PaymentGateway` port as the
  Razorpay adapter, injects labeled failure scenarios, and records what it
  injected in `simulator_ground_truth`. The evaluation harness scores the
  system against that ground truth.
- **Context:** "The system recovered ₹X" is an anecdote unless we know what was
  actually wrong and what should have been detected/recovered. Razorpay Test
  Mode cannot inject controlled failures (bank downtime, webhook loss, latency
  spikes) on demand, so live-test evidence alone can't produce precision/recall
  or recovery-rate numbers.
- **Options:**
  1. Evaluate only against Razorpay Test Mode behavior.
  2. Simulator with ground truth as the evaluation bed; Test Mode as the
     realism proof for execution paths.
  3. Hand-crafted fixtures per test.
- **Chosen:** (2).
- **Why:** Ground truth turns the demo into measurement: detection
  precision/recall/F1, diagnosis top-1 accuracy, MTTD/MTTR, recovery rate, and
  false-action rate all become computable (`evaluation_runs`). Because the
  simulator implements the identical `PaymentGateway` port, the entire loop —
  detection → policy → execution → webhook verification — runs unchanged; only
  the adapter is swapped (env `SIMULATION_MODE=true`, clearly separated from
  the Razorpay adapter, never mixed).
- **Tradeoffs:** Simulator fidelity bounds how much the numbers mean; scenario
  design effort; risk of overfitting heuristics to our own scenarios (mitigated
  by also demoing against Razorpay Test Mode).

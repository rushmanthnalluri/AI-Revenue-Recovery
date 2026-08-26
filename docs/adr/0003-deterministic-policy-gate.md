# ADR 0003: Deterministic policy engine gates all financial actions

- **Decision:** A deterministic, YAML-configured policy engine
  (`PolicyEngineProto.evaluate(ActionContext) -> PolicyDecision`) is the only
  path by which any financial action may be executed.
- **Context:** Recovery actions move real money (retries, payment links,
  subscription changes). AI components are probabilistic and can be wrong or
  manipulated; the buildathon rubric and payment-industry practice both demand
  that automation be bounded by explicit, auditable rules.
- **Options:**
  1. AI agent executes directly (tool-use autonomy).
  2. AI proposes, deterministic gate decides.
  3. All actions require manual approval.
- **Chosen:** (2).
- **Why:** Keeps the demo's AI autonomy story (low-risk actions auto-execute:
  confidence ≥ 0.85, ≤ ₹5000, attempts < 2) while making unsafe autonomy
  structurally impossible: refunds/irreversible actions/opted-out customers are
  hard-blocked, everything above thresholds requires human approval, and a
  stopping rule (3 consecutive failures per incident) halts runaway loops.
  Every evaluation is persisted immutably in `policy_decisions` with reasons
  and matched rules — full auditability.
- **Tradeoffs:** Some recoverable revenue waits on human approval; the policy
  file must be kept in sync with product intent; the engine is a single choke
  point that must stay dependency-free and fast (pure function over
  ActionContext + YAML config).

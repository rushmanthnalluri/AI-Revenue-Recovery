# ADR 0004: The reasoner never executes; LLM is optional and offline-first

- **Decision:** The AI reasoner (`ReasonerProto.investigate`) is advisory only,
  and the default implementation is an offline heuristic reasoner. An LLM
  provider can be enabled purely through env (`LLM_PROVIDER`, `OPENAI_*`).
- **Context:** Judges may run without network or API keys; LLM calls add
  latency, cost, and nondeterminism; and per ADR 0003 no probabilistic
  component may touch money. The investigation value-add (hypotheses,
  narrative, recommended actions) is useful even when heuristic.
- **Options:**
  1. LLM required for investigation.
  2. LLM optional behind env, heuristic default, both behind `ReasonerProto`.
  3. No AI investigation at all.
- **Chosen:** (2).
- **Why:** The system is fully demoable and testable offline (deterministic,
  free, fast), while the same port accepts an LLM-backed reasoner for richer
  narratives when keys exist. Both implementations produce the same
  `InvestigationReport` shape, and both feed the policy gate like any other
  proposal — the reasoner has no execution capability by construction (it
  receives evidence, returns a report, and holds no gateway/policy handles).
- **Tradeoffs:** Heuristic narratives are shallower than LLM output; two
  implementations to keep behaviorally aligned; LLM mode is nondeterministic,
  so evaluation runs always record `generated_by` and model name.

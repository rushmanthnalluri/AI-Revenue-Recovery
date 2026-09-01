# Agent-reasoning experiments

Track: investigation-agent reasoning quality and safety. Goal: measurably
improve what the reasoner claims, recommends, and escalates — while the
deterministic policy layer guarantees a confidently-wrong AI cannot move
money.

## How to reproduce

```
# run the versioned corpus (38 cases) and write records for an experiment
cd backend && .venv/Scripts/python scripts/agent_eval.py --exp-id <exp_id>

# pytest integration (locks the exp02 floors; runs with the default suite)
cd backend && .venv/Scripts/python -m pytest tests/agenteval -q
```

The runner (`backend/scripts/agent_eval.py`) seeds one fresh in-memory DB per
case (corpus version `agent-corpus-1.1`: 6 incident kinds + 6 edge cases x
{heuristic, llm-scripted} + 12 adversarial LLM cases, incl. 2 literal
prompt-injection-via-data cases added in 1.1), runs both reasoners
offline (the LLM path via scripted `chat_fn`), scores seven metrics plus a
zero-gateway-mutation invariant, and writes `config.json`, `metrics.json`,
`cases.json`, `failure_analysis.md` here. Metric definitions and the
adversarial matrix are in `docs/agent.md` ("Evaluation").

## Experiments

| exp | question | result |
|---|---|---|
| exp01_baseline | where does the agent stand on the seven metrics? | factual 1.0, policy 0.4917, unsafe-rate 0.75 (9/36 cases unsafe), expectations 17/23 — weaknesses: no_fault proposals, auto-lane previews on non-auto classes, missing gate outcomes in rationales, no opt-out filtering, BLOCKED refund headlined, model-confidence-only escalation |
| exp02_confidence_safety | do the targeted fixes (gate-confidence caps, no_fault→no_action, opt-out filtering, BLOCKED-drop, escalation floors, evidence-ceiling cap, advocacy strip, machine-checkable gate inputs, policy-outcome rationales) measurably help? | all seven metrics 1.0 except tool_call 0.9931 (rogue-attempt honesty), expectations 23/23, zero gateway mutations in all 36 cases; demo 10/10 and agent tests 34/34 green |
| exp04_ranked_candidates_injection | corpus 1.1: do ranked top-N candidate proposals (heuristic-1.2) and the structured guard checks (proposal-target grounding, confidence-vs-evidence-coverage) hold the floors, and are the 2 new literal prompt-injection-via-data cases caught? | all seven metrics 1.0 except tool_call 0.9934 (rogue-attempt honesty), expectations 29/29, zero gateway mutations in all 38 cases, reruns byte-identical; both injection cases degrade + strip + escalate, never headline the injected goal |

Scorer note: between exp01 and exp02 the scorer was refined once (advocacy
regex made imperative-aware; auto-lane check restricted to recovery actions;
opted-out LLM expectation set to escalate). The refinement was verified
score-neutral by re-running the pre-improvement code with the final scorer
(reproduced exp01 metrics before records were regenerated).

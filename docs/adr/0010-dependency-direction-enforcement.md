# ADR 0010: Dependency-direction enforcement via static import analysis

- **Decision:** The module dependency matrix is executable: an AST-based test
  (`backend/tests/architecture/test_boundaries.py`) statically verifies the
  sanctioned import directions over all of `app/**/*.py` on every test run.
- **Context:** ADR 0001 made the modular monolith work by promising
  microservice-grade separation: the policy engine is the deterministic core,
  the agent is advisory only, the gateway adapter is a leaf, the simulator
  must not depend on the system it feeds. Until now that matrix lived only in
  prose (`docs/architecture.md`, `docs/security-architecture.md`); nothing
  stopped a future change from wiring `agent -> razorpay` (LLM-adjacent code
  gaining a money-movement path) or `policy -> recovery` (the deterministic
  core depending on the probabilistic loop) and eroding the trust boundaries
  the whole safety model rests on. The seams that already existed — e.g. the
  evaluation harness importing `api.v1.webhooks.EVENT_HANDLERS` — show how
  easily layer shortcuts creep in without an executable rule.
- **Options:**
  1. AST-based static import analysis in the test suite (stdlib only).
  2. A third-party import-linter (e.g. `import-linter` contracts) — one more
     dependency and config dialect for the same guarantee.
  3. Code-review discipline only (the status quo).
- **Chosen:** (1).
- **Why:** Zero new dependencies, runs inside the suite everyone already runs,
  and fails with a readable violator report (`file:line  importer -> imported`
  plus the rule's rationale). AST analysis cannot be gamed by lazy or
  function-level imports the way a module-object inspection could, and it
  executes no application code. The rules table is data-driven, so a
  sanctioned exception is a one-line, reviewable diff — the test itself tells
  the author to get the exception sanctioned here rather than bypassed. A
  companion test forces every `app.services.*` package to be classified
  (ruled or explicitly exempted), so a new package cannot slip in unruled.
- **Tradeoffs:** The matrix is coarser than full layering (it forbids package
  directions, not individual symbol usage), and it only sees imports, not
  dynamic `importlib` trickery — accepted, since the goal is catching
  accidental erosion in review, not proving a negative. Sanctioned edges are
  now load-bearing documentation: agent -> services.{diagnosis, policy,
  revenue}, recovery -> services.{policy, revenue, razorpay.errors},
  detection -> app.schemas, and the evaluation harness as a second
  composition root (services + simulator + `app.api.v1.webhooks`) are
  explicitly allowed and encoded in the test's rule table.

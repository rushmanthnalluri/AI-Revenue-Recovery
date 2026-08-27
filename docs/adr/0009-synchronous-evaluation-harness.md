# ADR 0009: Synchronous evaluation harness

- **Decision:** The evaluation harness runs synchronously: the CLI blocks for
  the whole run, and `POST /api/v1/evaluation/run` holds the HTTP request
  until the baseline-vs-PulseRecover comparison completes.
- **Context:** A full-preset evaluation (30 simulated days, ~68k events, two
  arms in isolated scratch databases) takes minutes of wall clock; a reduced
  smoke run takes seconds. The monolith has no task queue or worker tier, and
  the evaluation is a demo/judging surface first: the caller wants the
  resulting metrics row, not a job id to poll.
- **Options:**
  1. Synchronous request/CLI that blocks until the run completes.
  2. Background job with a status endpoint and polling/webhook completion.
  3. Offline-only CLI, no API surface.
- **Chosen:** (1).
- **Why:** Simplicity and demonstrability: one command (or one curl) produces
  a fully populated `evaluation_runs` row with both arms' metrics — no queue
  infrastructure, no orphaned-job states, no partial results to explain. The
  two arms are already isolated in scratch SQLite databases, so a blocking
  call cannot corrupt serving state; the main database receives only the
  finished metrics rows.
- **Tradeoffs:** The API route is unsuitable for very large scenarios behind
  a proxy timeout (the CLI is the documented full-scale path); no concurrent
  run management; horizontal scaling of evaluation would require the worker
  tier this decision defers.

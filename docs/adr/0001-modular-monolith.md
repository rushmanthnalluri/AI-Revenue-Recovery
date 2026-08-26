# ADR 0001: Modular monolith

- **Decision:** Ship PulseRecover as a single-process modular monolith with
  port-defined module boundaries.
- **Context:** Buildathon timeline; multiple agents building in parallel; judges
  must run the whole system with one command; the domain (detection → diagnosis
  → recovery → verification) is a tightly coupled closed loop where network
  boundaries add latency and failure modes without buying isolation we need.
- **Options:**
  1. Microservices (one per loop stage).
  2. Modular monolith with enforced ports.
  3. Unstructured monolith.
- **Chosen:** (2) modular monolith.
- **Why:** One deployable and one database keep the demo and evaluation
  reproducible. Module boundaries are still real: integrations happen only
  through `app/ports.py` protocols and the shared models, so any module (e.g.
  the gateway adapter or the reasoner) could be extracted into a service later
  without changing its consumers. Parallel agents get clean file-level
  ownership (one router + one schema module + one service module each).
- **Tradeoffs:** No independent scaling or fault isolation between loop stages;
  discipline (ports only, no cross-module imports of internals) is enforced by
  convention and review, not by the network.

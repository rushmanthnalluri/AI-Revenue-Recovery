# ADR 0008: Opportunity-centric recovery API

- **Decision:** The recovery API is organized around per-payment
  `recovery_opportunities` (`/api/v1/recovery/opportunities/...`), not around
  incidents, batches, or campaigns. One opportunity = one failed payment or
  one abandoned checkout.
- **Context:** An incident's blast radius can be thousands of failed payments.
  The three hard requirements that shaped the model are: (1) verification —
  the webhook reconciler resolves outcomes through
  `recovery_opportunities.payment_id`, so each action must map to exactly one
  payment or no recovery can be proven; (2) policy precision — per-customer
  rate limits, duplicate protection, and opt-out blocks key off the
  opportunity's customer; (3) idempotency — re-running the builder after new
  webhook arrivals must add only the delta, which requires a stable
  per-(incident, payment) key.
- **Options:**
  1. Incident-level batch actions ("retry all 1,359 failures").
  2. Per-payment opportunities with per-opportunity plan/execute/approve.
  3. Campaign objects grouping opportunities with batch operations.
- **Chosen:** (2).
- **Why:** Per-payment granularity is what makes the closed loop provable:
  every `RECOVERED` action is tied to one gateway-confirmed payment, every
  policy decision is evaluated against one concrete amount/customer, and the
  audit trail reads as a ledger of individual decisions rather than batch
  summaries. Batch ergonomics are a UI concern (the console lists, filters,
  and compares opportunities); the API stays honest about the unit of
  execution and verification.
- **Tradeoffs:** Large incidents produce thousands of rows and list API
  traffic (pagination and filters mitigate); bulk human approval is N API
  calls, not one (acceptable — approval is meant to be a considered act);
  no first-class campaign rollups beyond the incident's aggregated stats.

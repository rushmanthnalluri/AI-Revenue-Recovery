# ADR 0006: Raw httpx REST client instead of the Razorpay SDK

- **Decision:** The `RazorpayGateway` adapter calls the Razorpay REST API
  directly over `httpx` (HTTP Basic auth, typed error mapping); the official
  Razorpay Python SDK is not used.
- **Context:** The adapter is the only component allowed to move money, so its
  exact wire behavior must be auditable and controllable: every mutation sent
  exactly once, ambiguous outcomes (timeout/5xx/unreadable) surfaced as typed
  errors rather than retried, backoff restricted to idempotent GETs, and
  Razorpay's `code`/`source`/`step`/`reason` error taxonomy preserved. The
  simulator twin must mirror the same semantics, which requires us to
  understand the wire contract precisely anyway. The SDK's convenience
  helpers (e.g. built-in retries/response wrapping) would put a third party's
  defaults on the execution path.
- **Options:**
  1. Official Razorpay Python SDK.
  2. Raw `httpx` REST client behind the `PaymentGateway` port.
  3. Razorpay MCP server as the execution transport.
- **Chosen:** (2).
- **Why:** Full control over the safety-critical semantics — one-mutation-ever,
  no hidden retries on ambiguous outcomes, backoff only on GETs, and direct
  access to the raw error envelope for the failure taxonomy that diagnosis and
  the simulator both depend on. The SDK's webhook-signature helper is trivial
  to replace (raw-body HMAC-SHA256, constant-time compare) and keeping it
  in-house removes a versioned dependency from the money path. The MCP server
  is execution plumbing with no detection/diagnosis/policy of its own, and
  adds a network hop we don't control.
- **Tradeoffs:** We re-implement and must maintain the envelope/error mapping
  ourselves (verified against the docs in docs/research.md); SDK feature
  additions (new APIs) require manual adapter work; new team members can't
  rely on SDK familiarity.

# PulseRecover — Security Architecture

**Companion:** `docs/architecture.md`, `docs/data-flow.md`, `docs/policy.md` (rule reference), `docs/security-testing.md` (adversarial attack matrix + proof suite).
**Prime directive (ADR 0003/0004):** probabilistic AI proposes; deterministic policy decides; the LLM never has authority over financial actions.

## 1. Trust boundaries

```
┌─────────────┐   HTTPS    ┌────────────────────── PulseRecover monolith ──────────────────────┐
│  Browser UI  │ ────────► │ API layer (routers)                                               │
└─────────────┘            │   ├─ agent service  ──tools whitelist──┐                          │
                           │   ├─ recovery service ────────────────┤                          │
 Razorpay ──webhook──────► │   ├─ webhook handlers                 │                          │
 (HMAC-gated)              │   └───────────────────────────────────▼                          │
                           │              DETERMINISTIC CORE (no LLM, no network)              │
                           │        policy engine · revenue engine · audit writer              │
                           │   ┌───────────────────────────────────┐                          │
 OpenAI-compatible LLM ◄── │   │ agent reasoners (advisory only)   │                          │
 (optional, outbound only) │   └───────────────────────────────────┘                          │
                           │ execution adapter: RazorpayGateway | SimulatedPaymentGateway      │
                           └──────────────────────────────────────────────────────────────────┘
```

- **LLM boundary:** the reasoner is a sandboxed advisor. It can reach only the 9-function `AgentTools` whitelist; it has no DB handle, no gateway import, no secrets in prompt context, and its two mutation tools must pass through `PolicyEngine.evaluate` before anything is created beyond a PROPOSED row. Amounts are copied from original payment/order rows — never from model output.
- **Gateway boundary:** all money movement crosses exactly one adapter behind `ports.PaymentGateway`; mutating calls carry `gateway_request_id` idempotency keys and are sent exactly once; ambiguity → UNKNOWN + GET-only resolution.
- **Webhook boundary:** unauthenticated ingress from Razorpay; bodies are size-capped at 1 MiB (413, enforced before verification), then every request is HMAC-verified against the raw body before any processing (fail-closed when no secret), deduplicated by `x-razorpay-event-id` UNIQUE constraint, and acknowledged fast with side effects confined to idempotent handlers.
- **Enforced continuously:** `backend/tests/architecture/test_boundaries.py` statically verifies the import-direction matrix (agent !→ gateway; policy !→ services; recovery !→ agent; adapter !→ policy/recovery; simulator !→ services) so these boundaries cannot silently erode.

## 2. Authentication & authorization (honest posture — demo grade)

- Mutating `/api/v1` routes require `X-API-Key` (env `API_KEY`, constant-time `hmac.compare_digest`); `/api/v1/demo` and `/api/v1/detection` are exempt when `APP_ENV != prod` (deterministic demo control); `/webhooks/razorpay` is HMAC-gated instead.
- **GETs are open by design** for the demo (read-only observability). Documented, deliberate.
- **Approver identity is self-declared** (`actor` in request body, e.g. `human:console`); audit records it verbatim. There is no SSO/RBAC — acceptable for a single-operator demo, listed as a known limitation with the production path (OIDC + role-bound actors + KYA-style principal binding, see `docs/product-strategy.md` P2).
- `APP_ENV` is a Literal without `"prod"`, so the API-key exemption can never be accidentally relaxed by config (the exemption checks `!= "prod"` and prod is unreachable).

## 3. Secrets & data protection

- Secrets are server-side env only (`RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `OPENAI_API_KEY`, `API_KEY`); `.env` gitignored; `.env.example` carries placeholders; the frontend receives only `NEXT_PUBLIC_API_KEY` for the demo key — Razorpay secrets never leave the backend.
- Structured logging redacts key/secret-like fields; API errors return a safe `{error:{code,message,request_id}}` envelope — no stack traces, no internals.
- PII minimization: simulator customers are synthetic; real-mode operation stores only Razorpay entity ids + metadata needed for recovery.
- Rate limiting: in-memory per-process (120/min webhooks, 60/min mutations) — noted as single-process-scoped.

## 4. Financial-action safety controls (defense in depth)

1. **Allowlist** — only enumerated action types; refunds are structurally impossible to auto-execute (`never_auto_execute`, tested with zero-gateway-call assertion).
2. **Thresholds** — auto-execute requires confidence ≥ 0.85 AND amount ≤ ₹5,000 AND attempts < 2 (i.e. first or second attempt); anything else → human approval.
3. **Stateful guards** — stopping rule (3 consecutive failures per incident/strategy → BLOCK), per-customer daily limits, duplicate cooldown, kill switch (exempt: escalate/no_action).
4. **Fail-closed** — malformed input, unknown action, broken config, missing history → BLOCKED or best-case REQUIRES_APPROVAL; auto-execution is structurally impossible in preview mode.
5. **Idempotency** — gateway dedup fields (receipt/reference_id), UNIQUE `gateway_request_id`, webhook event dedup, duplicate-execute protection (exactly-once wire assertion).
6. **Determinism & versioning** — same context → same decision; `policy_version` = sha256 of config; every decision persisted with matched rules.
7. **Audit** — every state transition recorded with actor, request-id, and policy reference.

## 5. AI-specific threats & mitigations

| Threat | Mitigation |
|---|---|
| LLM invents financial facts | All facts from deterministic tools; hallucination guard strips/flags any number not present in tool results; degraded fallback to heuristic reasoner |
| LLM triggers unsafe action | Mutation tools route through PolicyEngine; refund test proves BLOCKED with zero gateway calls |
| LLM exfiltrates secrets | No secrets in tool results or prompt context; outbound LLM call is the only network path and carries evidence payloads only |
| Prompt injection via payment metadata | Tool outputs are structured data, not instructions; reasoner treats them as evidence (LLM path) — heuristic default ignores content entirely |
| Overconfident automation | Confidence floors + approval gates + escalation when evidence insufficient; heuristic confidence capped at 0.7 (< 0.85 floor ⇒ approval lane without ML artifact) |

## 6. Residual risks (accepted, documented)

- Demo-grade authN/Z (§2). Production path: OIDC, role-bound approvers, principal-bound audit.
- Single-writer SQLite ceiling for local demo (Postgres path exists and is container-verified).
- In-memory gateway singleton + rate limiter are single-process; multi-worker deployment needs shared state (documented).
- No unattended reconciliation scheduler — operator-triggered sweep instead (ADR 0011); worker tier is P2.
- Failed webhook handlers ack 200 with `processed=false` (by design, so dedup can't swallow retries) and rely on the reconcile sweep.

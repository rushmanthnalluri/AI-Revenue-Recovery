# Audit Evidence — External Research for PulseRecover Roadmap

Captured: 2026-09-02 by the external-research audit agent.
Scope: Razorpay capabilities, competitor recovery tooling, dunning benchmarks, counterfactual measurement, agent-safety patterns, fintech observability UX.
Labels: **VERIFIED** (authoritative source fetched) / **VENDOR CLAIM** (vendor's own marketing/docs numbers) / **THIRD-PARTY** / **UNCERTAIN** / **NOT FOUND**.
This file is raw material for the roadmap — no feature recommendations.

---

## 0. Repo baseline (what the project already claims)

Local docs already carry a research pass dated 2026-08-26/27. Key claims the sections below re-verify or extend:

- `docs/research.md:116-131` — Razorpay capability map: Subscriptions fixed T+1/T+2/T+3 retries (`pending`→`halted`, missed cycles never re-attempted), Failed Payment Recovery links ("up to 20%" vendor claim), Intelligent Payment Retry, Payment Links reminders (≤3), Optimizer routing (~5% SR uplift claim), Agent Studio (4 agents incl. Subscription Recovery), SR Analytics + Downtime APIs, UPI-Autopay Intelligent Retry Engine (beta, merchant-configurable), Remote MCP 2.0 (35+ tools at mcp.razorpay.com).
- `docs/competitive-analysis.md:24-38` — adjacent-vendor matrix: Stripe Smart Retries ("not India-issued cards"; gross "by any means" recovery definition), Adyen Auto Rescue, Chargebee, Recurly, Butter, FlexPay→Revaly, Redux, Churnkey, Baremetrics Recover, Pagos.
- `docs/competitive-analysis.md:57-70` — measurement critique + benchmark ladder: gross attribution as market standard; Redux audit 25–35% vs Stripe 55% headline; Recurly soft-decline self-resolution 67–68% in 2–7 days; recovery ladder 38–42% (email) → 55% (Stripe) → 70–89% (specialists); Visa 15 / Mastercard 35 retry caps per 30 days; UPI ≈83% of India digital transactions (RBI).
- `docs/competitive-analysis.md:48-55` — guardrail landscape: AP2 signed mandates + non-agentic trusted surface; Stripe SPT / Mastercard Agentic Tokens; ACP rollback lesson; x402 micropayment scale; Lithic policy backtesting; Skyfire KYA.

Sections 1–6 below contain the fresh 2026-09-02 research pass.

---

## 1. Razorpay capabilities (current, recovery-relevant)

(pending delegated research)

## 2. Razorpay agentic/AI announcements

(pending delegated research)

## 3. Competitor recovery tooling (Stripe, Adyen, Checkout.com, dunning SaaS)

(pending delegated research)

## 4. Dunning/recovery benchmarks

(pending delegated research)

## 5. Counterfactual / holdout measurement of recovery lift

(pending delegated research)

## 6. Agent-safety patterns in fintech

(pending delegated research)

## 7. Fintech observability UX patterns

(pending delegated research)

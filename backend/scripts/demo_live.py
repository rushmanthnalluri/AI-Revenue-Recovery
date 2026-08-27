"""Live-demo beats against the DEPLOYED stack (docker compose).

`scripts/demo_run.py` drives the five scenarios in-process against a scratch
SQLite DB (the deterministic proof suite). This script is the complement for
the LIVE container demo (docs/demo-script.md): it performs the three beats
that cannot be done from the UI alone, against the deployed Postgres and the
deployed API — never a scratch world:

    captured  mint + POST a genuinely-signed payment.captured webhook for an
              opportunity's linked failed payment (the verification beat —
              the sim gateway answers create_order with status=created, so
              recovery is proven by the webhook, exactly like real Razorpay).
    beat-d    gateway timeout on the mutating call -> UNKNOWN, re-execute is a
              GET-only re-query (mutations stay at 1), then resolve RECOVERED
              on gateway evidence. Runs RecoveryExecutor in-process against
              the SAME Postgres the API serves, so every transition is visible
              in the UI and audit trail.
    beat-e    plant a compromised-AI refund strategy (the AI layer never
              generates refunds), then execute through the REAL HTTP API ->
              the deterministic gate BLOCKS it before any gateway call.

Run INSIDE the backend container (shares DATABASE_URL, i.e. the deployed
Postgres, and reaches the API over the compose network):

    docker compose -f deploy/docker-compose.yml exec backend \
        python scripts/demo_live.py captured --opportunity-id opp_...
    docker compose -f deploy/docker-compose.yml exec backend \
        python scripts/demo_live.py beat-d --incident-id inc_...
    docker compose -f deploy/docker-compose.yml exec backend \
        python scripts/demo_live.py beat-e --incident-id inc_...

From the host, point DATABASE_URL at the published Postgres port and
DEMO_API_BASE at the published backend port (same effect, more setup).
"""

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

import httpx
import sqlalchemy as sa

from app.config import settings
from app.db import SessionLocal
from app.models import (
    Customer,
    Payment,
    RecoveryAction,
    RecoveryOpportunity,
    RecoveryStrategy,
)
from app.ports import ActionType, RecoveryStatus
from app.services.recovery.executor import RecoveryExecutor
from app.services.razorpay.factory import DEFAULT_WEBHOOK_SECRET
from app.services.razorpay.simulated import SimulatedPaymentGateway
from app.services.revenue.classify import FailureClass, classify_failure

API_BASE = None  # set in main()


def say(text: str) -> None:
    # cp1252-safe printing on Windows consoles (mirrors demo_run.py).
    print(
        text.replace("—", "-").replace("–", "-").replace("→", "->")
        .encode("ascii", "replace")
        .decode("ascii")
    )


def rs(paise: int | None) -> str:
    """Integer paise -> 'Rs 9,41,483' (Indian digit grouping)."""
    if paise is None:
        return "Rs -"
    rupees = int(paise) // 100
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"Rs {s}"


# ---------------------------------------------------------------------------
# outage-capable, mutation-counting sim gateway (same twin the API uses)
# ---------------------------------------------------------------------------


class DemoGateway(SimulatedPaymentGateway):
    """Sim gateway + runtime outage switch + mutating-call counter.

    Mirrors scripts/demo_run.py's DemoGateway so beat-d proves 'exactly one
    mutating call was ever attempted' on the live stack too."""

    def __init__(self, **kw: Any) -> None:
        kw.setdefault("success_rate", 1.0)
        super().__init__(**kw)
        self.mutation_attempts = 0

    def set_outage(self, on: bool) -> None:
        if on:
            self._incident["outage"] = True
        else:
            self._incident.pop("outage", None)

    def create_order(self, **kw: Any) -> dict[str, Any]:
        self.mutation_attempts += 1
        return super().create_order(**kw)

    def create_payment_link(self, **kw: Any) -> dict[str, Any]:
        self.mutation_attempts += 1
        return super().create_payment_link(**kw)

    def create_subscription(self, **kw: Any) -> dict[str, Any]:
        self.mutation_attempts += 1
        return super().create_subscription(**kw)


# ---------------------------------------------------------------------------
# shared picking logic (deterministic; mirrors demo_run._pick_opportunity)
# ---------------------------------------------------------------------------


def _pick_open_opportunity(session, incident_id: str) -> dict[str, Any]:
    """Smallest auto-lane (<= Rs 5,000) retry-friendly opportunity on the
    incident with NO actions yet (a fresh execution slot). Ordering
    (amount_paise, payment id) is deterministic across runs."""
    rows = session.execute(
        sa.select(RecoveryOpportunity, Payment, Customer.opted_out)
        .join(Payment, RecoveryOpportunity.payment_id == Payment.id)
        .outerjoin(Customer, RecoveryOpportunity.customer_id == Customer.id)
        .where(RecoveryOpportunity.incident_id == incident_id)
        .order_by(RecoveryOpportunity.amount_paise, Payment.id)
    ).all()
    acted = set(
        session.scalars(
            sa.select(RecoveryAction.opportunity_id).where(
                RecoveryAction.opportunity_id.in_([opp.id for opp, _, _ in rows] or ["__none__"])
            )
        ).all()
    )
    candidates: list[dict[str, Any]] = []
    for opp, pay, opted_out in rows:
        if opted_out or opp.id in acted:
            continue
        cls = classify_failure(pay)
        if cls not in (FailureClass.SOFT_DECLINE, FailureClass.TIMEOUT):
            continue
        if opp.amount_paise > 500_000:  # auto-execute ceiling
            continue
        candidates.append(
            {
                "opportunity_id": opp.id,
                "payment_id": pay.id,
                "gateway_payment_id": pay.gateway_payment_id,
                "amount_paise": opp.amount_paise,
                "method": pay.method,
                "failure_class": cls.value,
            }
        )
    if not candidates:
        raise SystemExit(
            f"no fresh auto-lane opportunity found for incident {incident_id} "
            "(build opportunities first, and do not reuse a spent incident)"
        )
    readable = [c for c in candidates if c["amount_paise"] >= 50_000]
    return (readable or candidates)[0]


# ---------------------------------------------------------------------------
# captured: signed payment.captured webhook for the opportunity's payment
# ---------------------------------------------------------------------------


def cmd_captured(opportunity_id: str) -> None:
    session = SessionLocal()
    try:
        opp = session.get(RecoveryOpportunity, opportunity_id)
        if opp is None:
            raise SystemExit(f"opportunity not found: {opportunity_id}")
        pay = session.get(Payment, opp.payment_id) if opp.payment_id else None
        if pay is None or not pay.gateway_payment_id:
            raise SystemExit(f"opportunity {opportunity_id} has no gateway payment id")
        entity = {
            "id": pay.gateway_payment_id,
            "entity": "payment",
            "status": "captured",
            "captured": True,
            "amount": opp.amount_paise,
            "currency": "INR",
            "method": pay.method,
        }
    finally:
        session.close()

    secret = settings.RAZORPAY_WEBHOOK_SECRET or DEFAULT_WEBHOOK_SECRET
    gw = SimulatedPaymentGateway(webhook_secret=secret)
    body, signature, _minted_event_id = gw.build_event("payment.captured", entity)
    # build_event mints evt_simNNNNNN from a per-instance counter; this CLI
    # creates a fresh gateway per invocation, so every delivery would reuse
    # evt_sim000001 and later beats would be deduped as redeliveries. The
    # event id is a transport header (not part of the signed body), so derive
    # a deterministic per-payment id: unique per payment, stable across
    # demo passes.
    event_id = "evt_sim_" + hashlib.sha256(
        pay.gateway_payment_id.encode("utf-8")
    ).hexdigest()[:16]
    resp = httpx.post(
        f"{API_BASE}/webhooks/razorpay",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "x-razorpay-event-id": event_id,
        },
        timeout=15.0,
    )
    say(
        f"[VERIFY] POST /webhooks/razorpay payment.captured for {pay.gateway_payment_id} "
        f"(HMAC valid, event {event_id}) -> HTTP {resp.status_code} {resp.text}"
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# beat-d: timeout -> UNKNOWN -> no blind retry -> resolve RECOVERED
# ---------------------------------------------------------------------------


def cmd_beat_d(incident_id: str) -> None:
    session = SessionLocal()
    gw = DemoGateway()
    try:
        pick = _pick_open_opportunity(session, incident_id)
        opp_id = pick["opportunity_id"]
        say(
            f"[SETUP] opportunity {opp_id}: {rs(pick['amount_paise'])} {pick['method']} "
            f"{pick['failure_class']} (auto lane)"
        )
        executor = RecoveryExecutor(session, gw)

        say("[FAULT] injecting a gateway outage (mutations raise GatewayTransientError)")
        gw.set_outage(True)
        action = executor.execute(opp_id, actor="agent:strategist")
        session.commit()
        say(
            f"[EXECUTE] gateway timed out on the mutating call -> action {action.status.value} "
            f"({action.last_error})"
        )
        assert action.status is RecoveryStatus.UNKNOWN, action.status
        action_id = action.id
        assert gw.mutation_attempts == 1

        say("[RE-EXECUTE] operator retries the execute - must NOT re-fire the mutation")
        again = executor.execute(opp_id, actor="human:ops@pulserecover.demo")
        session.commit()
        say(
            f"        same action {again.id} re-queried instead of re-fired "
            f"(gateway mutations attempted: {gw.mutation_attempts} total; still {again.status.value})"
        )
        assert again.id == action_id and gw.mutation_attempts == 1
        assert again.status is RecoveryStatus.UNKNOWN

        say("[RECOVER] outage clears; the gateway's own records now show the payment captured")
        gw.set_outage(False)
        gw.payments[pick["gateway_payment_id"]] = {
            "id": pick["gateway_payment_id"],
            "entity": "payment",
            "status": "captured",
            "captured": True,
            "amount": pick["amount_paise"],
            "currency": "INR",
            "method": pick["method"],
        }
        final = executor.execute(opp_id, actor="human:ops@pulserecover.demo")
        session.commit()
        say(
            f"[RESOLVE] GET-only re-query (fetch_payment) proves the capture -> "
            f"action {final.status.value} - resolved on gateway evidence, never guessed"
        )
        assert final.status is RecoveryStatus.RECOVERED, final.status
        say(
            f"[RESULT] timeout -> UNKNOWN -> paused -> RECOVERED on evidence; exactly "
            f"{gw.mutation_attempts} mutating call was ever attempted "
            f"({rs(pick['amount_paise'])} at stake, never double-charged)"
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# beat-e: compromised AI proposes refund -> gate BLOCKS (zero gateway calls)
# ---------------------------------------------------------------------------


def cmd_beat_e(incident_id: str) -> None:
    session = SessionLocal()
    try:
        pick = _pick_open_opportunity(session, incident_id)
        opp_id = pick["opportunity_id"]
        refund = RecoveryStrategy(
            opportunity_id=opp_id,
            action_type=ActionType.REFUND,
            rank=99,
            expected_recovery_paise=pick["amount_paise"],
            confidence=0.99,
            risk="high",
            eligibility=True,
            reason="planted unsafe AI proposal: refund the payment in full",
            constraints={},
            generated_by="agent:compromised_llm",
            selected=False,
        )
        session.add(refund)
        session.commit()
        refund_id = refund.id
    finally:
        session.close()

    say(
        f"[ATTACK] a manipulated AI run proposes action_type=refund for "
        f"{rs(pick['amount_paise'])} on opportunity {opp_id} "
        f"(confidence 0.99 - confidence is not authority)"
    )
    resp = httpx.post(
        f"{API_BASE}/api/v1/recovery/{opp_id}/execute",
        json={"actor": "agent:compromised_llm", "strategy_id": refund_id},
        headers={"X-API-Key": settings.API_KEY},
        timeout=15.0,
    )
    resp.raise_for_status()
    res = resp.json()
    decision = res.get("policy_decision") or {}
    say(
        f"[POLICY] gate: {decision.get('outcome')} (rules: "
        f"{', '.join(decision.get('rules_matched') or [])}) - refund is not on the "
        f"allowlist and is in never_auto_execute; there is no approval lane"
    )
    say(
        f"[RESULT] action {res['status']} - {res['message']} "
        f"The gate blocks BEFORE any gateway call: zero money moved "
        f"(the CLI proof suite asserts the mutation counter stays at 0)."
    )
    if res["status"] != "REJECTED" or decision.get("outcome") != "BLOCKED":
        raise SystemExit(f"expected BLOCKED/REJECTED, got {res}")


# ---------------------------------------------------------------------------


def main() -> None:
    global API_BASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base",
        default=__import__("os").environ.get("DEMO_API_BASE", "http://backend:8000"),
        help="deployed API base URL (default: http://backend:8000 in-compose)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_cap = sub.add_parser("captured", help="mint+POST a signed payment.captured webhook")
    p_cap.add_argument("--opportunity-id", required=True)
    p_d = sub.add_parser("beat-d", help="gateway timeout -> UNKNOWN -> resolve")
    p_d.add_argument("--incident-id", required=True)
    p_e = sub.add_parser("beat-e", help="unsafe AI refund -> POLICY BLOCKED")
    p_e.add_argument("--incident-id", required=True)
    args = parser.parse_args()
    API_BASE = args.api_base.rstrip("/")

    if args.cmd == "captured":
        cmd_captured(args.opportunity_id)
    elif args.cmd == "beat-d":
        cmd_beat_d(args.incident_id)
    elif args.cmd == "beat-e":
        cmd_beat_e(args.incident_id)


if __name__ == "__main__":
    main()

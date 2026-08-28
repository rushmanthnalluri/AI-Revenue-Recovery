"""Invariant 7: a refund can NEVER autonomously execute.

Three independent layers, each mechanically checked:

1. POLICY: refund is absent from the shipped allowlist and present in
   never_auto_execute (proven exhaustively in
   tests/policy/test_config_loader.py and test_policy_engine.py — referenced
   from docs/payment-invariants.md).
2. EXECUTION: even with a hand-forged REFUND action row, the executor's
   dispatcher has no mapping for it — it raises a definitive error WITHOUT
   touching the gateway (defense in depth if the gate were ever bypassed).
3. TRANSPORT (this module's core proof): the PaymentGateway port and BOTH
   shipped implementations (real Razorpay REST client, simulator) expose no
   refund capability at all — there is no method to call, so no code path,
   buggy or adversarial, can move money backwards.

Behavioral proof that a full AI-proposed refund dies at the gate with zero
gateway calls: tests/recovery/test_failure_modes.py::TestRefundHasNoExecutionPath
and tests/agent/test_tools.py refund tests (referenced, not duplicated).
"""

from __future__ import annotations

import pytest

from app.ports import ActionType
from app.services.razorpay.client import RazorpayGateway
from app.services.razorpay.errors import GatewayClientError
from app.services.razorpay.simulated import SimulatedPaymentGateway
from app.services.recovery import RecoveryExecutor
from app.ports import PaymentGateway

from tests.security.conftest import CountingGateway


def _refundish_names(obj) -> list[str]:
    return [n for n in dir(obj) if "refund" in n.lower()]


def test_gateway_port_defines_no_refund_transport():
    assert _refundish_names(PaymentGateway) == [], (
        f"PaymentGateway port grew a refund capability: {_refundish_names(PaymentGateway)}"
    )


@pytest.mark.parametrize("cls", [RazorpayGateway, SimulatedPaymentGateway])
def test_gateway_implementations_have_no_refund_transport(cls):
    assert _refundish_names(cls) == [], (
        f"{cls.__name__} grew a refund capability: {_refundish_names(cls)}"
    )


def test_executor_dispatch_has_no_refund_mapping(
    db_session, make_opportunity, make_proposed_action
):
    """Even a forged REFUND action that somehow reached the dispatcher dies
    definitively (no side effects) — the transport layer is absent, not
    merely gated."""
    opp = make_opportunity()
    action = make_proposed_action(
        opp, action_type=ActionType.REFUND, confidence=0.99, amount_paise=100
    )
    db_session.commit()
    gateway = CountingGateway(success_rate=1.0)
    executor = RecoveryExecutor(db_session, gateway)

    with pytest.raises(GatewayClientError):
        executor._dispatch_gateway(action, opp)

    assert gateway.mutation_calls == 0
    assert gateway.fetch_calls == 0
    db_session.refresh(action)
    assert action.attempts == 0
    assert action.gateway_response is None

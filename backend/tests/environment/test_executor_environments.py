"""Environment isolation (d)+(e): gateway-by-environment execution semantics.

- A real_test opportunity executes against the REAL Razorpay adapter — never
  the simulator. With no real keys configured the executor refuses HONESTLY
  (typed GatewayNotConfiguredError -> 409 'razorpay_not_configured' at the
  API) with ZERO simulator calls; an injected real_gateway seam is honored.
- A research opportunity executes via the simulated twin (current behavior,
  unchanged).
"""

import pytest

from app.services.razorpay.simulated import SimulatedPaymentGateway
from app.services.recovery import (
    GatewayNotConfiguredError,
    RecoveryExecutor,
)
from app.ports import RecoveryStatus

API_KEY = {"X-API-Key": "dev-key"}


def _sim_call_count(gateway: SimulatedPaymentGateway) -> int:
    return len(gateway.orders) + len(gateway.payment_links) + len(gateway.subscriptions)


def _execute_through_approval(executor: RecoveryExecutor, opp_id: str):
    """Drive execute, taking the human-approval lane when the policy gate asks
    (mirrors the evaluation harness's operator)."""
    action = executor.execute(opp_id, actor="human:test")
    if action.status is RecoveryStatus.PENDING_APPROVAL:
        executor.approve(opp_id, actor="human:test", note="test operator")
        action = executor.execute(opp_id, actor="human:test")
    return action


def test_real_test_opportunity_refuses_without_real_keys(
    db_session, make_opportunity
):
    """No real keys anywhere -> honest typed refusal, zero gateway calls."""
    sim = SimulatedPaymentGateway(success_rate=1.0)
    opp = make_opportunity(environment="real_test")
    executor = RecoveryExecutor(db_session, sim)  # no real_gateway seam, settings have no keys

    with pytest.raises(GatewayNotConfiguredError) as excinfo:
        _execute_through_approval(executor, opp.id)
    assert excinfo.value.code == "razorpay_not_configured"
    assert excinfo.value.status_code == 409
    assert _sim_call_count(sim) == 0  # the simulator was NEVER touched


def test_real_test_opportunity_refusal_surfaces_as_409_via_api(
    client, db_session, make_opportunity
):
    """The API maps the refusal to 409 with the razorpay_not_configured code
    (the client fixture's settings run SIMULATION_MODE with no keys)."""
    opp = make_opportunity(environment="real_test")
    r = client.post(
        f"/api/v1/recovery/{opp.id}/execute",
        json={"actor": "human:test"},
        headers=API_KEY,
    )
    if r.status_code == 200 and r.json()["status"] == "PENDING_APPROVAL":
        r = client.post(
            f"/api/v1/recovery/{opp.id}/approve",
            json={"actor": "human:test"},
            headers=API_KEY,
        )
        assert r.status_code == 200, r.text
        r = client.post(
            f"/api/v1/recovery/{opp.id}/execute",
            json={"actor": "human:test"},
            headers=API_KEY,
        )
    assert r.status_code == 409, r.text
    assert "razorpay_not_configured" in r.text


def test_real_test_opportunity_uses_the_injected_real_gateway(
    db_session, make_opportunity
):
    """The seam: an injected real_gateway receives the mutation; the research
    (injected default) gateway stays untouched."""
    sim = SimulatedPaymentGateway(success_rate=1.0)
    real = SimulatedPaymentGateway(success_rate=1.0)  # stands in for RazorpayGateway
    opp = make_opportunity(environment="real_test")
    executor = RecoveryExecutor(db_session, sim, real_gateway=real)

    action = _execute_through_approval(executor, opp.id)
    db_session.commit()

    assert action.status in (RecoveryStatus.RECOVERED, RecoveryStatus.VERIFYING)
    assert action.environment == "real_test"
    assert _sim_call_count(real) >= 1  # the real seam got exactly the mutation
    assert _sim_call_count(sim) == 0


def test_research_opportunity_executes_via_the_simulated_twin(
    db_session, make_opportunity
):
    """Current behavior intact: research -> the injected (simulated) gateway."""
    sim = SimulatedPaymentGateway(success_rate=1.0)
    opp = make_opportunity(environment="research")
    executor = RecoveryExecutor(db_session, sim)

    action = _execute_through_approval(executor, opp.id)
    db_session.commit()

    assert action.status in (RecoveryStatus.RECOVERED, RecoveryStatus.VERIFYING)
    assert action.environment == "research"
    assert _sim_call_count(sim) >= 1

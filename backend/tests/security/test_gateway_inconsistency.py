"""Attack vectors: inconsistent/malicious gateway responses and gateway hangs.

- Identity confusion: fetch_payment/fetch_order answer for a DIFFERENT id
  than requested. resolve() must NOT mark the action RECOVERED on someone
  else's captured payment — "verification proves" requires identity checks.
- Malformed success bodies (200 with error envelope, non-JSON, missing id).
- Hanging gateway during the reconcile sweep: every fetch is bounded by the
  httpx timeout + retry budget; the sweep finishes and actions stay UNKNOWN.
"""

from __future__ import annotations

import time

import httpx
import pytest
import sqlalchemy as sa

import app.models as models
from app.ports import RecoveryStatus
from app.services.razorpay.client import RazorpayGateway
from app.services.razorpay.errors import GatewayTransientError
from app.services.recovery import RecoveryExecutor, run_reconciliation

from tests.security.conftest import ConfusedGateway, HangingGateway


class TestResolveIdentityConfusion:
    """REGRESSION: resolve() used to trust the STATUS of a fetch response
    without verifying the returned entity id matches the requested id — a
    confused gateway could flip UNKNOWN actions to RECOVERED with somebody
    else's captured payment (false recovered revenue)."""

    def test_fetch_payment_wrong_id_does_not_resolve_recovered(
        self, db_session, make_payment, make_opportunity, make_unknown_action
    ):
        payment = make_payment(gateway_payment_id="pay_original_failed")
        opp = make_opportunity(payment=payment)
        action = make_unknown_action(opp)

        gateway = ConfusedGateway()
        executor = RecoveryExecutor(db_session, gateway)
        executor.resolve(action.id, actor="system:attack")
        db_session.commit()

        assert gateway.requested_payment_ids == ["pay_original_failed"]
        assert action.status is RecoveryStatus.UNKNOWN, (
            "resolve() marked RECOVERED on a payment id it never asked for"
        )
        # The mismatch itself must be auditable.
        row = db_session.scalar(
            sa.select(models.AuditLog).where(
                models.AuditLog.action == "recovery.action.resolve_check",
                models.AuditLog.entity_id == action.id,
            )
        )
        assert row is not None
        assert "mismatch" in str(row.details).lower()

    def test_fetch_order_wrong_id_does_not_resolve_recovered(
        self, db_session, make_opportunity, make_unknown_action
    ):
        opp = make_opportunity()
        action = make_unknown_action(
            opp, gateway_response={"id": "order_created_by_action"}
        )

        gateway = ConfusedGateway()
        executor = RecoveryExecutor(db_session, gateway)
        executor.resolve(action.id, actor="system:attack")
        db_session.commit()

        assert gateway.requested_order_ids == ["order_created_by_action"]
        assert action.status is RecoveryStatus.UNKNOWN

    def test_fetch_payment_matching_id_still_recovers(
        self, db_session, make_payment, make_opportunity, make_unknown_action
    ):
        """The fix must not break the legitimate path: a captured response
        for the REQUESTED id still proves recovery."""

        class HonestGateway(ConfusedGateway):
            def fetch_payment(self, payment_id: str):
                self.requested_payment_ids.append(payment_id)
                return {
                    "id": payment_id,  # identity verified
                    "entity": "payment",
                    "status": "captured",
                    "captured": True,
                    "amount": 100_000,
                }

        payment = make_payment(gateway_payment_id="pay_now_captured")
        opp = make_opportunity(payment=payment)
        action = make_unknown_action(opp)

        executor = RecoveryExecutor(db_session, HonestGateway())
        executor.resolve(action.id, actor="system:attack")
        db_session.commit()
        assert action.status is RecoveryStatus.RECOVERED


class TestMalformedGatewayResponses:
    """200-with-error-envelope and non-JSON success bodies must map to
    transient (ambiguous) errors, never to silent success or a crash."""

    @staticmethod
    def _gateway_with(handler) -> RazorpayGateway:
        return RazorpayGateway(
            key_id="rzp_test_key",
            key_secret="secret",
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,
        )

    def test_200_with_error_envelope_is_transient_not_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "weird"}}
            )

        gw = self._gateway_with(handler)
        with pytest.raises(GatewayTransientError):
            gw.fetch_payment("pay_x")

    def test_200_non_json_body_is_transient(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>ok</html>")

        gw = self._gateway_with(handler)
        with pytest.raises(GatewayTransientError):
            gw.fetch_order("order_x")

    def test_200_json_missing_id_is_transient(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"entity": "payment", "status": "captured"})

        gw = self._gateway_with(handler)
        with pytest.raises(GatewayTransientError):
            gw.fetch_payment("pay_x")


class TestTimeoutsBounded:
    """A hanging gateway must never cause an unbounded wait — on execute
    (already proven), on resolve, and across the whole reconcile sweep."""

    def test_httpx_client_has_explicit_timeout(self):
        gw = RazorpayGateway(key_id="k", key_secret="s")
        timeout = gw._http.timeout
        assert timeout.connect is not None and timeout.read is not None
        assert timeout.read <= 30.0

    def test_hanging_gateway_raises_transient_within_timeout(self):
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            # What a genuinely hung gateway surfaces as once the configured
            # httpx timeout fires (MockTransport cannot enforce socket timeouts).
            raise httpx.ReadTimeout("read timed out", request=request)

        gw = RazorpayGateway(
            key_id="k",
            key_secret="s",
            timeout_seconds=0.2,
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,  # don't really back off in tests
        )
        start = time.perf_counter()
        with pytest.raises(GatewayTransientError):
            gw.fetch_payment("pay_x")
        elapsed = time.perf_counter() - start
        # The idempotent GET retries a bounded number of times, then gives up.
        assert attempts == 3, f"expected 3 bounded attempts, saw {attempts}"
        assert elapsed < 1.5, f"unbounded wait: {elapsed:.2f}s"

    def test_mutating_call_never_retried_on_timeout(self):
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("read timed out", request=request)

        gw = RazorpayGateway(
            key_id="k",
            key_secret="s",
            timeout_seconds=0.2,
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,
        )
        with pytest.raises(GatewayTransientError):
            gw.create_order(amount_paise=100, idempotency_key="gwr_x")
        assert attempts == 1, (
            f"a mutating POST was retried after a timeout ({attempts} attempts) — "
            "double-charge risk"
        )

    def test_reconcile_sweep_completes_with_hanging_gateway(
        self, db_session, make_payment, make_opportunity, make_unknown_action
    ):
        gateway = HangingGateway()
        actions = []
        for i in range(3):
            payment = make_payment(gateway_payment_id=f"pay_hang_{i}")
            opp = make_opportunity(payment=payment)
            actions.append(make_unknown_action(opp))

        start = time.perf_counter()
        report = run_reconciliation(db_session, gateway, actor="human:ops")
        elapsed = time.perf_counter() - start

        assert report.unknown_scanned == 3
        assert report.resolved == 0
        assert report.still_unknown == 3
        assert all(a.status is RecoveryStatus.UNKNOWN for a in actions)
        # The doubles raise immediately; the assertion that matters is that
        # the sweep TERMINATES and never mutates the gateway.
        assert elapsed < 10.0
        assert gateway.mutation_calls == 0

    def test_sweep_audit_row_written_even_when_gateway_hangs(
        self, db_session, make_opportunity, make_unknown_action
    ):
        make_unknown_action(make_opportunity())
        run_reconciliation(db_session, HangingGateway(), actor="human:ops")
        row = db_session.scalar(
            sa.select(models.AuditLog).where(models.AuditLog.action == "recovery.reconcile")
        )
        assert row is not None

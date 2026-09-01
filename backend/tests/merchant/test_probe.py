"""Connection-probe tests: keys present? authenticated GET /v1/payments?count=1?
401 -> connected=false typed; network error -> typed; masked key id only.
All gateway traffic is fixture-backed (httpx.MockTransport, see conftest).
"""

import httpx

from app.services.merchant import SyncService
from app.services.merchant.service import environment_for_key, mask_key_id
from tests.merchant.conftest import (
    KEY_ID,
    KEY_SECRET,
    FakeRazorpayAPI,
    error_response,
)


def test_probe_ok(sync_service: SyncService, fake_api: FakeRazorpayAPI) -> None:
    probe = sync_service.probe_connection()
    assert probe.configured is True
    assert probe.connected is True
    assert probe.environment == "test"
    assert probe.key_id_masked == "rzp_test_••••ID01"
    assert probe.connection_error is None
    # The probe is exactly the documented authenticated ping.
    assert fake_api.requests == [("payments", {"count": "1"})]


def test_probe_401_authentication_failed(
    sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    fake_api.failures["payments"] = error_response(401, "Your api key/secret is invalid")
    probe = sync_service.probe_connection()
    assert probe.configured is True
    assert probe.connected is False
    assert probe.connection_error == "authentication_failed"


def test_probe_network_error_unreachable(
    sync_service: SyncService, fake_api: FakeRazorpayAPI
) -> None:
    fake_api.failures["payments"] = httpx.ConnectError("fixture: connection refused")
    probe = sync_service.probe_connection()
    assert probe.configured is True
    assert probe.connected is False
    assert probe.connection_error == "unreachable"


def test_probe_not_configured_without_keys() -> None:
    probe = SyncService(key_id="", key_secret="").probe_connection()
    assert probe.configured is False
    assert probe.connected is False
    assert probe.environment is None
    assert probe.key_id_masked is None
    assert probe.connection_error is None


def test_probe_not_configured_in_simulation_mode(fake_api: FakeRazorpayAPI) -> None:
    """SIMULATION_MODE refuses to touch the network even with keys present
    (mirrors factory.get_real_gateway)."""
    service = SyncService(
        key_id=KEY_ID,
        key_secret=KEY_SECRET,
        simulation_mode=True,
        transport=httpx.MockTransport(fake_api.handler),
    )
    probe = service.probe_connection()
    assert probe.configured is False
    assert probe.connected is False
    assert fake_api.requests == []  # zero network I/O happened


def test_probe_live_key_environment(fake_api: FakeRazorpayAPI) -> None:
    service = SyncService(
        key_id="rzp_live_fixtureLIVE9",
        key_secret=KEY_SECRET,
        transport=httpx.MockTransport(fake_api.handler),
        sleep=lambda _s: None,
    )
    probe = service.probe_connection()
    assert probe.environment == "live"
    assert probe.key_id_masked == "rzp_live_••••IVE9"


def test_environment_for_key() -> None:
    assert environment_for_key("rzp_test_x") == "test"
    assert environment_for_key("rzp_live_x") == "live"
    assert environment_for_key("something_else") is None
    assert environment_for_key("") is None


def test_mask_key_id_never_exposes_middle() -> None:
    assert mask_key_id("rzp_test_abcdef123456") == "rzp_test_••••3456"
    assert mask_key_id("rzp_live_abcdef123456") == "rzp_live_••••3456"
    assert mask_key_id("weirdkey1234567") == "••••4567"
    assert mask_key_id("abc") == "••••"
    assert mask_key_id("") is None
    # The masked form shares at most prefix+last4 with the original.
    masked = mask_key_id(KEY_ID)
    assert KEY_SECRET not in (masked or "")
    assert masked != KEY_ID

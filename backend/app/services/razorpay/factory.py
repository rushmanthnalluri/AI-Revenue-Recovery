"""Gateway factory: pick the real Razorpay adapter or the simulation twin.

Selection rule (mirrored by /api/v1/system/health via `gateway_mode`):
- `SIMULATION_MODE=true`, or no `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET`
  configured -> SimulatedPaymentGateway (never touches the network).
- otherwise -> RazorpayGateway against the configured base URL (test mode is
  selected by the `rzp_test_*` key, not the URL).
"""

from functools import lru_cache

from app.config import Settings
from app.ports import PaymentGateway
from app.services.razorpay.client import RazorpayGateway
from app.services.razorpay.simulated import DEFAULT_WEBHOOK_SECRET, SimulatedPaymentGateway


def use_simulator(settings: Settings) -> bool:
    """True when the app must run against the simulation twin."""
    return settings.SIMULATION_MODE or not (
        settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET
    )


def gateway_mode(settings: Settings) -> str:
    """Human-readable mode for health reporting: 'simulator' | 'razorpay_test'."""
    return "simulator" if use_simulator(settings) else "razorpay_test"


@lru_cache(maxsize=4)
def _real_gateway(
    key_id: str, key_secret: str, base_url: str, webhook_secret: str
) -> RazorpayGateway:
    return RazorpayGateway(
        key_id=key_id,
        key_secret=key_secret,
        base_url=base_url,
        webhook_secret=webhook_secret,
    )


@lru_cache(maxsize=4)
def _sim_gateway(webhook_secret: str) -> SimulatedPaymentGateway:
    return SimulatedPaymentGateway(webhook_secret=webhook_secret)


def get_gateway(settings: Settings) -> PaymentGateway:
    """Return the process-wide PaymentGateway for these settings."""
    if use_simulator(settings):
        return _sim_gateway(settings.RAZORPAY_WEBHOOK_SECRET or DEFAULT_WEBHOOK_SECRET)
    return _real_gateway(
        settings.RAZORPAY_KEY_ID,
        settings.RAZORPAY_KEY_SECRET,
        settings.RAZORPAY_BASE_URL,
        settings.RAZORPAY_WEBHOOK_SECRET,
    )


def get_real_gateway(settings: Settings) -> RazorpayGateway | None:
    """The REAL Razorpay adapter for real_test execution, or None when real
    keys are not configured (SIMULATION_MODE on / keys missing). Never returns
    the simulator — the caller must fail honestly on None (the recovery
    executor refuses with `razorpay_not_configured`)."""
    if use_simulator(settings):
        return None
    return _real_gateway(
        settings.RAZORPAY_KEY_ID,
        settings.RAZORPAY_KEY_SECRET,
        settings.RAZORPAY_BASE_URL,
        settings.RAZORPAY_WEBHOOK_SECRET,
    )


__all__ = ["get_gateway", "get_real_gateway", "use_simulator", "gateway_mode"]

"""Razorpay integration: real test-mode adapter + simulation twin.

- `client.RazorpayGateway` — raw REST (httpx, basic auth), no SDK.
- `simulated.SimulatedPaymentGateway` — SIMULATION ONLY, seeded/deterministic.
- `factory.get_gateway(settings)` — picks real vs simulated.
- `errors` — typed gateway errors mapped from the Razorpay error envelope.
"""

from app.services.razorpay.client import RazorpayGateway
from app.services.razorpay.factory import gateway_mode, get_gateway, use_simulator
from app.services.razorpay.simulated import SimulatedPaymentGateway

__all__ = [
    "RazorpayGateway",
    "SimulatedPaymentGateway",
    "get_gateway",
    "use_simulator",
    "gateway_mode",
]

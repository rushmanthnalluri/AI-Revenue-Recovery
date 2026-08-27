"""Shared FastAPI dependency seams for the API layer.

`get_gateway_dependency` is THE single gateway seam: routers declare it with
`Depends(...)` and tests override the one function object via
`app.dependency_overrides[get_gateway_dependency]`. It used to be duplicated
in `api/v1/webhooks.py` and `api/v1/recovery.py` — two distinct function
objects meant overriding one seam silently left the other bound to the
environment gateway.
"""

from app.config import settings
from app.ports import PaymentGateway
from app.services.razorpay.factory import get_gateway


def get_gateway_dependency() -> PaymentGateway:
    """FastAPI dependency seam — tests override this with a fixed gateway."""
    return get_gateway(settings)


__all__ = ["get_gateway_dependency"]

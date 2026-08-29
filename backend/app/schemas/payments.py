"""Payments schemas — the environment-scoped commerce read surface."""

from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import Paginated


class PaymentSummary(BaseModel):
    """One payment row with its real (gateway-shaped) fields. ``source_type``
    is the provenance stamp the environment scope is derived from."""

    id: str
    external_id: str | None = None  # upstream id (Razorpay pay_* or simulator id)
    gateway_payment_id: str | None = None
    order_id: str | None = None
    gateway_order_id: str | None = None
    merchant_id: str
    customer_id: str | None = None
    amount_paise: int
    currency: str = "INR"
    method: str | None = None
    status: str
    error_code: str | None = None
    error_description: str | None = None
    error_source: str | None = None
    captured: bool = False
    created_at: datetime
    source_type: str = "simulator"  # simulator | razorpay_test | razorpay_live


class PaymentListResponse(Paginated[PaymentSummary]):
    pass

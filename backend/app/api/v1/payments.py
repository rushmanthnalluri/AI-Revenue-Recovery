"""Payments endpoints — the environment-scoped commerce read surface.

GET /api/v1/payments lists payments for ONE environment at a time (default
'real_test' — the REAL MERCHANT mode; 'research' is the simulator sandbox).
The scope is derived from the row's ``source_type`` provenance stamp:
'razorpay_test'/'razorpay_live' -> real_test, 'simulator' -> research
(app.models.base.source_types_for_environment). A ``source_type`` filter
outside the requested environment's set intersects to an empty page — a
research row can never surface through a real_test query.
"""

from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Order, Payment, source_types_for_environment
from app.schemas.payments import PaymentListResponse, PaymentSummary

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


@router.get("", response_model=PaymentListResponse)
def list_payments(
    db: Session = Depends(get_db),
    environment: Literal["real_test", "research"] = Query(default="real_test"),
    status: str | None = Query(default=None),
    method: str | None = Query(default=None),
    source_type: str | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> PaymentListResponse:
    filters = [Payment.source_type.in_(source_types_for_environment(environment))]
    if status is not None:
        filters.append(Payment.status == status)
    if method is not None:
        filters.append(Payment.method == method)
    if source_type is not None:
        filters.append(Payment.source_type == source_type)
    if from_ is not None:
        filters.append(Payment.created_at >= from_)
    if to is not None:
        filters.append(Payment.created_at <= to)

    total = int(
        db.scalar(sa.select(sa.func.count()).select_from(Payment).where(*filters)) or 0
    )
    rows = db.execute(
        sa.select(Payment, Order.gateway_order_id)
        .outerjoin(Order, Payment.order_id == Order.id)
        .where(*filters)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return PaymentListResponse(
        items=[
            PaymentSummary(
                id=payment.id,
                external_id=payment.external_id,
                gateway_payment_id=payment.gateway_payment_id,
                order_id=payment.order_id,
                gateway_order_id=gateway_order_id,
                merchant_id=payment.merchant_id,
                customer_id=payment.customer_id,
                amount_paise=payment.amount_paise,
                currency=payment.currency or "INR",
                method=payment.method,
                status=payment.status,
                error_code=payment.error_code,
                error_description=payment.error_description,
                error_source=payment.error_source,
                captured=payment.captured,
                created_at=payment.created_at,
                source_type=payment.source_type,
            )
            for payment, gateway_order_id in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )

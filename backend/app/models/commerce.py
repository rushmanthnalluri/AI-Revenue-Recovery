"""Commerce domain: merchants, customers, orders, payments, payment events,
subscriptions. Mirrors the Razorpay object model closely enough to reconcile
gateway state, with local prefixed ids as primary keys and gateway ids kept as
separate unique columns."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app import ids
from app.db import Base, TZDateTime
from app.models.base import ProvenanceMixin, TimestampMixin


class Merchant(ProvenanceMixin, TimestampMixin, Base):
    __tablename__ = "merchants"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.merchant_id)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(sa.String(255))
    gateway_account_id: Mapped[str | None] = mapped_column(sa.String(64))  # razorpay account
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)

    customers: Mapped[list["Customer"]] = relationship(back_populates="merchant")


class Customer(ProvenanceMixin, TimestampMixin, Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.customer_id)
    merchant_id: Mapped[str] = mapped_column(
        sa.ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str | None] = mapped_column(sa.String(255))
    phone: Mapped[str | None] = mapped_column(sa.String(32))
    name: Mapped[str | None] = mapped_column(sa.String(255))
    gateway_customer_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    # Hard policy input: opted-out customers must never be auto-contacted.
    opted_out: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)

    merchant: Mapped[Merchant] = relationship(back_populates="customers")


class Order(ProvenanceMixin, TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.order_id)
    merchant_id: Mapped[str] = mapped_column(
        sa.ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    gateway_order_id: Mapped[str | None] = mapped_column(sa.String(64), unique=True, index=True)
    amount_paise: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(8), default="INR", nullable=False)
    status: Mapped[str] = mapped_column(sa.String(32), default="created", nullable=False)
    receipt: Mapped[str | None] = mapped_column(sa.String(128))
    meta: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)


class Payment(ProvenanceMixin, TimestampMixin, Base):
    __tablename__ = "payments"
    # Dedup per upstream source: the same Razorpay/simulator payment id can
    # never be stored twice under one source_type (NULL external_id rows are
    # always distinct, matching the gateway_payment_id unique semantics).
    __table_args__ = (
        sa.UniqueConstraint("source_type", "external_id", name="uq_payments_source_external"),
    )

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.payment_id)
    merchant_id: Mapped[str] = mapped_column(
        sa.ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    gateway_payment_id: Mapped[str | None] = mapped_column(sa.String(64), unique=True, index=True)
    amount_paise: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(8), default="INR", nullable=False)
    # Gateway-native fields kept as plain strings (created/authorized/captured/
    # failed/refunded; method: upi/card/netbanking/wallet/emi/...).
    method: Mapped[str | None] = mapped_column(sa.String(32), index=True)
    status: Mapped[str] = mapped_column(sa.String(32), default="created", nullable=False, index=True)
    error_code: Mapped[str | None] = mapped_column(sa.String(64))
    error_description: Mapped[str | None] = mapped_column(sa.Text)
    error_source: Mapped[str | None] = mapped_column(sa.String(32))  # gateway/bank/network/customer
    captured: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    gateway_created_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    meta: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)

    events: Mapped[list["PaymentEvent"]] = relationship(
        back_populates="payment", cascade="all, delete-orphan"
    )


class PaymentEvent(ProvenanceMixin, TimestampMixin, Base):
    """Append-only event stream per payment — the raw signal for detection."""

    __tablename__ = "payment_events"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.payment_event_id)
    payment_id: Mapped[str] = mapped_column(
        sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(sa.String(32))
    to_status: Mapped[str | None] = mapped_column(sa.String(32))
    # source: poller | webhook | simulator | seed
    source: Mapped[str] = mapped_column(sa.String(32), default="poller", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False, index=True)

    payment: Mapped[Payment] = relationship(back_populates="events")


class Subscription(ProvenanceMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.subscription_id)
    merchant_id: Mapped[str] = mapped_column(
        sa.ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    gateway_subscription_id: Mapped[str | None] = mapped_column(sa.String(64), unique=True, index=True)
    plan_id: Mapped[str | None] = mapped_column(sa.String(64))
    # created/authenticated/active/paused/halted/cancelled/completed
    status: Mapped[str] = mapped_column(sa.String(32), default="created", nullable=False, index=True)
    amount_paise: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(8), default="INR", nullable=False)
    period: Mapped[str | None] = mapped_column(sa.String(16))  # daily/weekly/monthly/yearly
    current_period_start: Mapped[datetime | None] = mapped_column(TZDateTime())
    current_period_end: Mapped[datetime | None] = mapped_column(TZDateTime())
    retry_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)

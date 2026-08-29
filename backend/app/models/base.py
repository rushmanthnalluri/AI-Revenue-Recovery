"""Shared column helpers for all models.

Portable across SQLite and Postgres (ADR 0002): enums are VARCHAR+CHECK
(native_enum=False), JSON uses sa.JSON, datetimes use TZDateTime.
Money is always integer paise (column names end in `_paise`).
"""

from datetime import datetime
from enum import Enum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import TZDateTime, utcnow


def enum_col(enum: type[Enum], name: str, **kw: Any) -> Mapped[Enum]:
    """Portable str-enum column (VARCHAR + CHECK on both SQLite and Postgres)."""
    return mapped_column(
        sa.Enum(
            enum,
            name=name,
            native_enum=False,
            length=32,
            validate_strings=True,
        ),
        **kw,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )


# Provenance source_type values — plain strings, same style as
# PaymentEvent.source (see docs/data-provenance.md).
SOURCE_TYPE_SIMULATOR = "simulator"
SOURCE_TYPE_RAZORPAY_TEST = "razorpay_test"
SOURCE_TYPE_RAZORPAY_LIVE = "razorpay_live"
SIMULATOR_SOURCE_SYSTEM = "pulserecover-simulator"
RAZORPAY_SOURCE_SYSTEM = "razorpay"


class ProvenanceMixin:
    """Data provenance: which source wrote this row (docs/data-provenance.md).

    source_type is 'simulator' | 'razorpay_test' | 'razorpay_live'; the
    'simulator' server default keeps pre-provenance rows honestly tagged.
    external_id is the upstream id (Razorpay pay_/order_/sub_ id or the
    simulator's deterministic gateway id). ingested_at is when the row
    entered this database (created_at may be a simulated-window timestamp).
    """

    source_type: Mapped[str] = mapped_column(
        sa.String(32),
        default=SOURCE_TYPE_SIMULATOR,
        server_default=SOURCE_TYPE_SIMULATOR,
        nullable=False,
        index=True,
    )
    source_system: Mapped[str | None] = mapped_column(sa.String(64))
    external_id: Mapped[str | None] = mapped_column(sa.String(64))
    ingested_at: Mapped[datetime] = mapped_column(
        TZDateTime(), default=utcnow, nullable=False
    )

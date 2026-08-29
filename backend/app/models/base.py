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

# Environment vocabulary — the strict boundary between REAL MERCHANT mode
# (Razorpay Test Mode data) and RESEARCH mode (simulator data). Commerce rows
# carry no environment column; their environment is DERIVED from source_type
# via this mapping. Derived tables (incidents, recovery, audit, ...) carry the
# environment column directly (migration b4e7a1c2d305).
ENVIRONMENT_REAL_TEST = "real_test"
ENVIRONMENT_RESEARCH = "research"
KNOWN_ENVIRONMENTS: tuple[str, ...] = (ENVIRONMENT_REAL_TEST, ENVIRONMENT_RESEARCH)

_ENVIRONMENT_SOURCE_TYPES: dict[str, tuple[str, ...]] = {
    ENVIRONMENT_REAL_TEST: (SOURCE_TYPE_RAZORPAY_TEST, SOURCE_TYPE_RAZORPAY_LIVE),
    ENVIRONMENT_RESEARCH: (SOURCE_TYPE_SIMULATOR,),
}


def source_types_for_environment(environment: str) -> tuple[str, ...]:
    """Commerce ``source_type`` values belonging to an environment — the only
    sanctioned mapping between the two provenance axes."""
    try:
        return _ENVIRONMENT_SOURCE_TYPES[environment]
    except KeyError:
        raise ValueError(
            f"unknown environment {environment!r} (known: {', '.join(KNOWN_ENVIRONMENTS)})"
        ) from None


class EnvironmentMixin:
    """Which environment a derived row belongs to: ``real_test`` (REAL
    MERCHANT mode, Razorpay Test Mode data) or ``research`` (simulator data).

    The 'research' default is the safe failure direction: a writer that
    forgets to stamp lands in the research sandbox and can never leak into a
    real_test query; every existing row predates real ingestion and is
    honestly simulator-derived (docs/data-provenance.md)."""

    environment: Mapped[str] = mapped_column(
        sa.String(16),
        default=ENVIRONMENT_RESEARCH,
        server_default=ENVIRONMENT_RESEARCH,
        nullable=False,
        index=True,
    )


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

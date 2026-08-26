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

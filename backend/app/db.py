"""Database engine, session factory, declarative Base, and helpers.

- SQLite by default (`sqlite:///./pulserecover.db`), overridable via DATABASE_URL
  (e.g. Postgres in docker-compose). Only portable SQLAlchemy types are used in
  models — no Postgres-only types (see docs/adr/0002).
- All datetimes are timezone-aware UTC. `TZDateTime` normalizes naive values on
  the way in and attaches UTC on the way out (SQLite returns naive datetimes).
"""

from datetime import datetime, timezone
from typing import Generator

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class TZDateTime(sa.TypeDecorator):
    """DateTime that guarantees tz-aware UTC values in both directions."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


def _connect_args(url: str) -> dict:
    return {"check_same_thread": False} if url.startswith("sqlite") else {}


engine = sa.create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args(settings.DATABASE_URL),
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

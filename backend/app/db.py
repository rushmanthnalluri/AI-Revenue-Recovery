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
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    if url.startswith("postgresql"):
        # Fail fast (3s) instead of psycopg's indefinite retry when the DB is
        # unreachable — readiness endpoints must answer `database: down`
        # promptly (demo-chaos finding F1).
        return {"connect_timeout": 3}
    return {}


def enable_sqlite_fk(engine: sa.engine.Engine) -> None:
    """Enforce SQLite foreign-key constraints on every new connection.

    SQLite ships with per-connection FK enforcement OFF; Postgres always
    enforces. Turn it on so FK-ordering bugs (child row inserted before its
    parent) fail fast in tests/dev instead of only on production Postgres.
    No-op for non-SQLite backends.
    """
    if engine.url.get_backend_name() != "sqlite":
        return

    @sa.event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_conn, _):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


engine = sa.create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args(settings.DATABASE_URL),
    pool_pre_ping=True,
)
enable_sqlite_fk(engine)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

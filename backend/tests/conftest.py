"""Test fixtures: in-memory SQLite, TestClient, ORM factories.

The app is built once per test session with get_db overridden to a shared
in-memory SQLite connection (StaticPool), so tests never touch the file DB.
"""

from collections.abc import Generator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db, utcnow
from app.main import create_app
import app.models as models


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session: Session) -> Generator[TestClient, None, None]:
    app = create_app()

    def _override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c


# --- factory helpers ---------------------------------------------------------

@pytest.fixture()
def make_merchant(db_session: Session):
    def _make(**kw) -> models.Merchant:
        m = models.Merchant(name=kw.pop("name", "Test Merchant"), **kw)
        db_session.add(m)
        db_session.commit()
        return m

    return _make


@pytest.fixture()
def make_payment(db_session: Session, make_merchant):
    def _make(merchant=None, **kw) -> models.Payment:
        merchant = merchant or make_merchant()
        p = models.Payment(
            merchant_id=merchant.id,
            amount_paise=kw.pop("amount_paise", 50000),
            status=kw.pop("status", "failed"),
            **kw,
        )
        db_session.add(p)
        db_session.commit()
        return p

    return _make


@pytest.fixture()
def make_incident(db_session: Session):
    def _make(**kw) -> models.Incident:
        inc = models.Incident(
            title=kw.pop("title", "Payment success rate drop"),
            metric=kw.pop("metric", "payment_success_rate"),
            detected_at=kw.pop("detected_at", utcnow()),
            **kw,
        )
        db_session.add(inc)
        db_session.commit()
        return inc

    return _make

"""Integration-test fixtures: TestClient wired to an in-memory DB and a
SimulatedPaymentGateway (both gateway dependency seams overridden)."""

from collections.abc import Generator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_gateway_dependency
from app.db import Base, get_db
from app.main import create_app
from app.services.razorpay.simulated import SimulatedPaymentGateway


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from app.db import enable_sqlite_fk

    enable_sqlite_fk(engine)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def make_client(db_session: Session):
    """Client factory: `make_client(gateway=...)` to control gateway outcomes."""

    def _make(gateway: SimulatedPaymentGateway | None = None) -> TestClient:
        gateway = gateway or SimulatedPaymentGateway(success_rate=1.0)
        app = create_app()

        def _override_get_db() -> Generator[Session, None, None]:
            yield db_session

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_gateway_dependency] = lambda: gateway
        return TestClient(app)

    return _make


@pytest.fixture()
def client(make_client) -> Generator[TestClient, None, None]:
    with make_client() as c:
        yield c


API_KEY = {"X-API-Key": "dev-key"}

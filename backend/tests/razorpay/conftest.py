"""Fixtures for razorpay adapter/webhook tests.

The `client` fixture shadows the root one: it additionally overrides the
gateway dependency with a SimulatedPaymentGateway holding a known webhook
secret, so webhook tests can produce genuinely valid signatures.
"""

import hashlib
import hmac
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_gateway_dependency
from app.db import get_db
from app.main import create_app
from app.services.razorpay.simulated import SimulatedPaymentGateway

WH_SECRET = "whsec_test_secret"


@pytest.fixture()
def gateway() -> SimulatedPaymentGateway:
    return SimulatedPaymentGateway(webhook_secret=WH_SECRET)


@pytest.fixture()
def client(db_session: Session, gateway: SimulatedPaymentGateway) -> Generator[TestClient, None, None]:
    app = create_app()

    def _override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gateway_dependency] = lambda: gateway
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def sign():
    def _sign(body: bytes, secret: str = WH_SECRET) -> str:
        return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    return _sign

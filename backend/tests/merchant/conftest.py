"""Fixtures for merchant-sync tests.

Every Razorpay payload, key, and endpoint behavior in this package is a
**TEST FIXTURE** served through `httpx.MockTransport` — no network, no real
credentials. Payload shapes follow the verified entity docs in
docs/razorpay-integration.md §C/§B (collection envelope
`{"entity":"collection","count":N,"items":[...]}`).
"""

import json
from collections.abc import Generator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.merchant import get_sync_service
from app.db import get_db
from app.main import create_app
from app.services.merchant import SyncService

# --- fixture credentials (NOT real secrets; the secret must never leak) ------
KEY_ID = "rzp_test_fixtureKEYID01"
KEY_SECRET = "fixture-secret-NEVER-real-0123456789"
API_KEY_HEADERS = {"X-API-Key": "dev-key"}

FIXTURE_TS = 1_700_000_000  # fixed unix timestamp used by all fixture payloads


# --- fixture entity payloads (docs §C field sets) ----------------------------


def order_payload(rid: str = "order_FixOrd01", **over: Any) -> dict[str, Any]:
    """TEST FIXTURE: a Razorpay order entity."""
    payload: dict[str, Any] = {
        "id": rid,
        "entity": "order",
        "amount": 50_000,
        "amount_paid": 0,
        "amount_due": 50_000,
        "currency": "INR",
        "receipt": "act_fixture",
        "status": "created",
        "attempts": 0,
        "notes": {},
        "created_at": FIXTURE_TS,
    }
    payload.update(over)
    return payload


def payment_payload(rid: str = "pay_FixPay01", **over: Any) -> dict[str, Any]:
    """TEST FIXTURE: a Razorpay payment entity."""
    payload: dict[str, Any] = {
        "id": rid,
        "entity": "payment",
        "amount": 50_000,
        "currency": "INR",
        "status": "captured",
        "method": "upi",
        "order_id": "order_FixOrd01",
        "captured": True,
        "email": "fixture@example.com",
        "contact": "+919999999999",
        "fee": 1180,
        "tax": 180,
        "refund_status": None,
        "amount_refunded": 0,
        "international": False,
        "error_code": None,
        "error_description": None,
        "error_source": None,
        "error_step": None,
        "error_reason": None,
        "notes": {},
        "created_at": FIXTURE_TS,
    }
    payload.update(over)
    return payload


def subscription_payload(rid: str = "sub_FixSub01", **over: Any) -> dict[str, Any]:
    """TEST FIXTURE: a Razorpay subscription entity (no amount field — §C)."""
    payload: dict[str, Any] = {
        "id": rid,
        "entity": "subscription",
        "plan_id": "plan_FixPlan01",
        "customer_id": "cust_FixCust01",
        "status": "active",
        "current_start": FIXTURE_TS,
        "current_end": FIXTURE_TS + 2_592_000,
        "total_count": 12,
        "paid_count": 3,
        "remaining_count": 9,
        "quantity": 1,
        "auth_attempts": 0,
        "source": "api",
        "notes": {},
        "created_at": FIXTURE_TS,
    }
    payload.update(over)
    return payload


def payment_link_payload(rid: str = "plink_FixPlink1", **over: Any) -> dict[str, Any]:
    """TEST FIXTURE: a Razorpay payment-link entity."""
    payload: dict[str, Any] = {
        "id": rid,
        "entity": "payment_link",
        "amount": 50_000,
        "amount_paid": 50_000,
        "currency": "INR",
        "reference_id": "act_fixturelink",
        "status": "paid",
        "accept_partial": False,
        "notes": {},
        "created_at": FIXTURE_TS,
    }
    payload.update(over)
    return payload


# --- fixture gateway ----------------------------------------------------------


class FakeRazorpayAPI:
    """TEST FIXTURE: in-memory stand-in for the Razorpay list endpoints.

    Applies count/skip slicing like the real API, serves payment_links by
    reference_id, records every request, and can be programmed to fail per
    collection with an httpx.Response or an exception (network errors).
    """

    def __init__(self) -> None:
        self.collections: dict[str, list[dict[str, Any]]] = {
            "orders": [],
            "payments": [],
            "subscriptions": [],
        }
        self.payment_links: dict[str, list[dict[str, Any]]] = {}
        self.failures: dict[str, httpx.Response | Exception] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        collection = request.url.path.rstrip("/").split("/")[-1]
        params = dict(request.url.params)
        self.requests.append((collection, params))
        failure = self.failures.get(collection)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return failure
        if collection == "payment_links":
            items = self.payment_links.get(params.get("reference_id", ""), [])
            return httpx.Response(
                200,
                json={"entity": "collection", "count": len(items), "items": items},
            )
        if collection in self.collections:
            count = int(params.get("count", "10"))
            skip = int(params.get("skip", "0"))
            items = self.collections[collection][skip : skip + count]
            return httpx.Response(
                200,
                json={"entity": "collection", "count": len(items), "items": items},
            )
        return httpx.Response(
            404, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "unknown"}}
        )


@pytest.fixture()
def fake_api() -> FakeRazorpayAPI:
    return FakeRazorpayAPI()


@pytest.fixture()
def sync_service(fake_api: FakeRazorpayAPI) -> SyncService:
    """A SyncService wired to the fixture gateway (page_size=2 exercises
    pagination with tiny fixtures)."""
    return SyncService(
        key_id=KEY_ID,
        key_secret=KEY_SECRET,
        transport=httpx.MockTransport(fake_api.handler),
        sleep=lambda _s: None,
        page_size=2,
    )


@pytest.fixture()
def client(db_session: Session, sync_service: SyncService) -> Generator[TestClient, None, None]:
    app = create_app()

    def _override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_sync_service] = lambda: sync_service
    with TestClient(app) as c:
        yield c


def error_response(status: int, description: str) -> httpx.Response:
    """TEST FIXTURE: a Razorpay error envelope (docs §H)."""
    return httpx.Response(
        status,
        content=json.dumps(
            {"error": {"code": "BAD_REQUEST_ERROR", "description": description}}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )

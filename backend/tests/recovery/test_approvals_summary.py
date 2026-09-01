"""Whole-queue aggregate for the pending-approvals lane.

The Approval Center's 'Value awaiting decision' metric sums page 1 of
GET /opportunities?status=PENDING_APPROVAL (page_size 50) client-side.
GET /opportunities/approvals-summary computes COUNT + SUM SQL-side over the
ENTIRE queue, scoped to one environment — these tests pin correctness beyond
page 1 and environment isolation.
"""

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_gateway_dependency
from app.db import get_db
from app.main import create_app
from app.ports import RecoveryStatus


@pytest.fixture()
def api_client(db_session, sim_gateway):
    app = create_app()

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gateway_dependency] = lambda: sim_gateway
    with TestClient(app) as c:
        yield c


class TestApprovalsSummary:
    def test_empty_queue_is_zero(self, api_client):
        resp = api_client.get(
            "/api/v1/recovery/opportunities/approvals-summary",
            params={"environment": "research"},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "environment": "research",
            "status": "PENDING_APPROVAL",
            "pending_count": 0,
            "pending_amount_paise": 0,
        }

    def test_aggregates_beyond_page_one(self, api_client, db_session, make_opportunity):
        # 60 pending opportunities — past the panel's page-1 window of 50.
        for _ in range(60):
            make_opportunity(status=RecoveryStatus.PENDING_APPROVAL, amount_paise=1_000)
        # noise that must NOT be counted: other statuses
        make_opportunity(status=RecoveryStatus.PROPOSED, amount_paise=999_999)
        make_opportunity(status=RecoveryStatus.RECOVERED, amount_paise=999_999)
        db_session.commit()

        resp = api_client.get(
            "/api/v1/recovery/opportunities/approvals-summary",
            params={"environment": "research"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pending_count"] == 60
        assert body["pending_count"] > 50  # the page-1 window this replaces
        assert body["pending_amount_paise"] == 60_000

        # the list endpoint agrees on count but cannot carry the sum
        page1 = api_client.get(
            "/api/v1/recovery/opportunities",
            params={
                "environment": "research",
                "status": "PENDING_APPROVAL",
                "page": 1,
                "page_size": 50,
            },
        ).json()
        assert page1["total"] == 60
        assert sum(item["amount_paise"] for item in page1["items"]) == 50_000

    def test_environments_never_mix(self, api_client, db_session, make_opportunity):
        for _ in range(3):
            make_opportunity(
                status=RecoveryStatus.PENDING_APPROVAL,
                amount_paise=500,
                environment="real_test",
            )
        for _ in range(7):
            make_opportunity(
                status=RecoveryStatus.PENDING_APPROVAL,
                amount_paise=100,
                environment="research",
            )
        db_session.commit()

        real = api_client.get(
            "/api/v1/recovery/opportunities/approvals-summary",
            params={"environment": "real_test"},
        ).json()
        assert real["pending_count"] == 3
        assert real["pending_amount_paise"] == 1_500

        research = api_client.get(
            "/api/v1/recovery/opportunities/approvals-summary",
            params={"environment": "research"},
        ).json()
        assert research["pending_count"] == 7
        assert research["pending_amount_paise"] == 700

    def test_read_only_needs_no_api_key_and_response_shape(self, api_client, db_session, make_opportunity):
        make_opportunity(status=RecoveryStatus.PENDING_APPROVAL, amount_paise=42)
        db_session.commit()
        resp = api_client.get(
            "/api/v1/recovery/opportunities/approvals-summary"
        )  # default environment=real_test, no X-API-Key header
        assert resp.status_code == 200
        assert set(resp.json()) == {
            "environment",
            "status",
            "pending_count",
            "pending_amount_paise",
        }

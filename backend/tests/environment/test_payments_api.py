"""Environment isolation (f): GET /api/v1/payments — environment scoping via
source_type, filters, from/to window, and pagination."""

from datetime import timedelta

from app.db import utcnow


def _seed_mixed(db_session, make_payment, make_real_payment):
    # research (simulator) rows
    sim1 = make_payment(status="captured", captured=True, method="upi", amount_paise=10_000)
    sim2 = make_payment(status="failed", method="card", amount_paise=20_000)
    sim3 = make_payment(status="failed", method="upi", amount_paise=30_000)
    # real_test (razorpay_test) rows
    real1 = make_real_payment(status="captured", captured=True, method="upi", amount_paise=40_000)
    real2 = make_real_payment(status="failed", method="netbanking", amount_paise=50_000)
    return sim1, sim2, sim3, real1, real2


def test_default_scope_is_real_test(client, db_session, make_payment, make_real_payment):
    _, _, _, real1, real2 = _seed_mixed(db_session, make_payment, make_real_payment)

    r = client.get("/api/v1/payments")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    ids = {p["id"] for p in body["items"]}
    assert ids == {real1.id, real2.id}
    for item in body["items"]:
        assert item["source_type"] == "razorpay_test"
        # real fields are present in the contract
        for field in (
            "id",
            "external_id",
            "gateway_payment_id",
            "order_id",
            "amount_paise",
            "method",
            "status",
            "error_code",
            "error_description",
            "error_source",
            "created_at",
            "source_type",
        ):
            assert field in item


def test_research_scope_returns_only_simulator_rows(
    client, db_session, make_payment, make_real_payment
):
    sim1, sim2, sim3, _, _ = _seed_mixed(db_session, make_payment, make_real_payment)
    body = client.get("/api/v1/payments", params={"environment": "research"}).json()
    assert body["total"] == 3
    assert {p["id"] for p in body["items"]} == {sim1.id, sim2.id, sim3.id}
    assert {p["source_type"] for p in body["items"]} == {"simulator"}


def test_status_and_method_filters(client, db_session, make_payment, make_real_payment):
    _seed_mixed(db_session, make_payment, make_real_payment)

    body = client.get("/api/v1/payments", params={"status": "failed"}).json()
    assert body["total"] == 1
    assert body["items"][0]["method"] == "netbanking"

    body = client.get("/api/v1/payments", params={"method": "upi"}).json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "captured"

    body = client.get(
        "/api/v1/payments", params={"environment": "research", "status": "failed"}
    ).json()
    assert body["total"] == 2


def test_source_type_filter_intersects_with_environment(
    client, db_session, make_payment, make_real_payment
):
    """Negative: a source_type outside the requested environment yields an
    empty page — research rows can never surface through a real_test query."""
    _seed_mixed(db_session, make_payment, make_real_payment)

    body = client.get("/api/v1/payments", params={"source_type": "simulator"}).json()
    assert body["total"] == 0
    assert body["items"] == []

    body = client.get(
        "/api/v1/payments",
        params={"environment": "research", "source_type": "razorpay_test"},
    ).json()
    assert body["total"] == 0

    body = client.get("/api/v1/payments", params={"source_type": "razorpay_test"}).json()
    assert body["total"] == 2


def test_from_to_window_filter(client, db_session, make_payment, make_real_payment):
    _seed_mixed(db_session, make_payment, make_real_payment)
    now = utcnow()

    body = client.get(
        "/api/v1/payments", params={"from": (now - timedelta(minutes=1)).isoformat()}
    ).json()
    assert body["total"] == 2

    body = client.get(
        "/api/v1/payments", params={"to": (now - timedelta(days=1)).isoformat()}
    ).json()
    assert body["total"] == 0

    body = client.get(
        "/api/v1/payments",
        params={
            "from": (now - timedelta(days=1)).isoformat(),
            "to": (now + timedelta(days=1)).isoformat(),
        },
    ).json()
    assert body["total"] == 2


def test_pagination(client, db_session, make_payment, make_real_payment):
    _seed_mixed(db_session, make_payment, make_real_payment)

    page1 = client.get("/api/v1/payments", params={"page": 1, "page_size": 1}).json()
    assert page1["total"] == 2 and page1["page"] == 1 and len(page1["items"]) == 1
    page2 = client.get("/api/v1/payments", params={"page": 2, "page_size": 1}).json()
    assert page2["page"] == 2 and len(page2["items"]) == 1
    assert page1["items"][0]["id"] != page2["items"][0]["id"]
    page3 = client.get("/api/v1/payments", params={"page": 3, "page_size": 1}).json()
    assert page3["items"] == []

    assert client.get("/api/v1/payments", params={"page_size": 0}).status_code == 422
    assert client.get("/api/v1/payments", params={"environment": "moon"}).status_code == 422

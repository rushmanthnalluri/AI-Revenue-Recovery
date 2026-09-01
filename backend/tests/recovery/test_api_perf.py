"""Query-count regressions for the recovery read API (N+1 fixes).

- GET /opportunities: the displayed status is the latest action's status, so
  each row used to lazy-load `opp.actions` — one extra query PER ROW (up to
  200 at max page size). Now selectinload: constant 3 SELECTs (count, page,
  actions) at any page size.
- GET /{opportunity_id}: `_action_view` used to re-query the latest policy
  decision PER ACTION. Now two IN-queries total for the whole action list.

Counts are asserted with a SQLAlchemy `before_cursor_execute` listener on the
shared test engine, and O(1)-ness is proven by comparing counts at two
different row counts.
"""

from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import app.models as models
from app.api.deps import get_gateway_dependency
from app.db import get_db, utcnow
from app.main import create_app
from app.ports import ActionType, PolicyOutcome, RecoveryStatus

API_KEY = {"X-API-Key": "dev-key"}

SUMMARY_KEYS = {
    "id", "incident_id", "payment_id", "customer_id", "subscription_id",
    "opportunity_type", "status", "amount_paise", "currency",
    "expected_recovery_paise", "confidence", "risk", "reason",
    "created_at", "expires_at", "environment",
}

ACTION_KEYS = {
    "id", "opportunity_id", "strategy_id", "action_type", "status",
    "amount_paise", "currency", "confidence", "actor", "attempts",
    "gateway_request_id", "policy_decision", "proposed_at", "executed_at",
    "verified_at", "completed_at", "approved_by", "note", "last_error",
}


@pytest.fixture()
def api_client(db_session, sim_gateway):
    app = create_app()

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gateway_dependency] = lambda: sim_gateway
    with TestClient(app) as c:
        yield c


@contextmanager
def count_selects(db_session):
    """Count SELECT statements issued on the test engine inside the block."""
    engine = db_session.get_bind()
    counts = {"select": 0}

    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counts["select"] += 1

    sa.event.listen(engine, "before_cursor_execute", _count)
    try:
        yield counts
    finally:
        sa.event.remove(engine, "before_cursor_execute", _count)


class TestListOpportunitiesQueryCount:
    def test_constant_queries_regardless_of_row_count(
        self, api_client, db_session, make_opportunity, make_proposed_action
    ):
        for _ in range(5):
            make_proposed_action(make_opportunity())
        db_session.commit()

        with count_selects(db_session) as small:
            resp = api_client.get(
                "/api/v1/recovery/opportunities",
                params={"environment": "research", "page_size": 5},
            )
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 5

        for _ in range(20):
            make_proposed_action(make_opportunity())
        db_session.commit()

        with count_selects(db_session) as large:
            resp = api_client.get(
                "/api/v1/recovery/opportunities",
                params={"environment": "research", "page_size": 25},
            )
        assert len(resp.json()["items"]) == 25

        # 1 count + 1 page select + 1 selectinload for actions — at BOTH
        # sizes. Pre-fix this grew by one query per row (5 vs 25).
        assert small["select"] == 3
        assert large["select"] == 3

    def test_response_shape_unchanged(
        self, api_client, db_session, make_opportunity, make_proposed_action
    ):
        opp = make_opportunity()
        make_proposed_action(opp, status=RecoveryStatus.PENDING_APPROVAL)
        db_session.commit()
        resp = api_client.get(
            "/api/v1/recovery/opportunities", params={"environment": "research"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"items", "total", "page", "page_size"}
        (item,) = body["items"]
        assert set(item) == SUMMARY_KEYS
        # projected status still comes from the latest action
        assert item["status"] == "PENDING_APPROVAL"


class TestDetailQueryCount:
    def _seed_actions(self, db_session, opp, make_proposed_action):
        """6 actions: one with a LINKED decision, one with two action_id-keyed
        decisions (latest wins), the rest with none."""
        linked_action = make_proposed_action(opp)
        keyed_action = make_proposed_action(opp, action_type=ActionType.NOTIFY_CUSTOMER)
        for _ in range(4):
            make_proposed_action(opp, action_type=ActionType.NO_ACTION)

        older = models.PolicyDecisionRecord(
            action_id=keyed_action.id,
            action_type=keyed_action.action_type.value,
            outcome=PolicyOutcome.REQUIRES_APPROVAL,
            actor="system:test",
            decided_at=utcnow(),
        )
        db_session.add(older)
        db_session.flush()
        newer = models.PolicyDecisionRecord(
            action_id=keyed_action.id,
            action_type=keyed_action.action_type.value,
            outcome=PolicyOutcome.ALLOWED,
            actor="system:test",
            decided_at=utcnow(),
        )
        db_session.add(newer)
        linked = models.PolicyDecisionRecord(
            action_id=None,  # reachable ONLY via the action's FK-style link
            action_type=linked_action.action_type.value,
            outcome=PolicyOutcome.REQUIRES_APPROVAL,
            actor="system:test",
            decided_at=utcnow(),
        )
        db_session.add(linked)
        db_session.flush()
        linked_action.policy_decision_id = linked.id
        db_session.commit()
        return linked_action, keyed_action, linked, newer

    def test_decisions_batch_prefetched_and_shape_preserved(
        self, api_client, db_session, make_opportunity, make_proposed_action
    ):
        opp = make_opportunity()
        linked_action, keyed_action, linked, newer = self._seed_actions(
            db_session, opp, make_proposed_action
        )

        with count_selects(db_session) as counts:
            resp = api_client.get(f"/api/v1/recovery/{opp.id}")
        assert resp.status_code == 200
        # opp(1) + actions(1) + linked decisions IN(1) + action_id decisions
        # IN(1) + audit(1). Pre-fix: +1 query per action (6 more).
        assert counts["select"] <= 5

        body = resp.json()
        assert len(body["actions"]) == 6
        for view in body["actions"]:
            assert set(view) == ACTION_KEYS
        by_id = {a["id"]: a for a in body["actions"]}
        # linked record wins (exactly latest_policy_decision semantics)…
        assert by_id[linked_action.id]["policy_decision"]["id"] == linked.id
        # …and the newest action_id-keyed record is the fallback
        assert by_id[keyed_action.id]["policy_decision"]["id"] == newer.id
        no_decision = [
            a for a in body["actions"]
            if a["id"] not in {linked_action.id, keyed_action.id}
        ]
        assert all(a["policy_decision"] is None for a in no_decision)

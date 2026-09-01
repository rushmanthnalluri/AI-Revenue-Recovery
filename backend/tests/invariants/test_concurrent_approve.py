"""Invariant: one pending approval -> at most one APPROVED transition, even
when two approve requests race.

Sibling of test_concurrent_execute.py (same race, human lane): approve used a
plain get_opportunity read while execute took the SELECT ... FOR UPDATE row
lock (RecoveryExecutor._lock_opportunity), so two racing approvers could both
read PENDING_APPROVAL and both stamp APPROVED. All five human-decision entry
points (approve/reject/escalate/cancel, plus execute) now take the row lock:
on Postgres the loser's lock wait ends after the winner's commit, it re-reads
APPROVED, and is cleanly refused with 409.

SQLite has no row locks (with_for_update is silently omitted, and its writer
serialization does NOT order two UPDATEs of the same row — verified live: a
barrier-synchronized double approve double-approves on SQLite). The loser
thread below is therefore serialized explicitly: it fires only after the
winner's response lands — the exact interleaving FOR UPDATE enforces on
Postgres — and the test proves the guard itself: the second approve is
refused, exactly one APPROVED audit row exists. (The single-writer SQLite
ceiling for genuinely simultaneous writes is accepted risk #5 in
docs/security-testing.md; the Postgres path is the production one, see
test_concurrent_execute.py's docstring.)
"""

from __future__ import annotations

import threading

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app.models as models
from app.db import Base, get_db, utcnow
from app.main import create_app
from app.ports import ActionType, RecoveryStatus

API_KEY = {"X-API-Key": "dev-key"}


def _seed_pending_approval(session) -> tuple[str, str]:
    """One opportunity with a PENDING_APPROVAL action awaiting a human.
    Returns (opportunity_id, action_id)."""
    opp = models.RecoveryOpportunity(
        opportunity_type="failed_payment_retry",
        status=RecoveryStatus.PENDING_APPROVAL,
        amount_paise=100_000,
    )
    session.add(opp)
    session.flush()
    action = models.RecoveryAction(
        opportunity_id=opp.id,
        action_type=ActionType.CREATE_PAYMENT_LINK,
        status=RecoveryStatus.PENDING_APPROVAL,
        amount_paise=100_000,
        actor="agent:strategist",
        proposed_at=utcnow(),
        decided_at=utcnow(),
    )
    session.add(action)
    session.commit()
    return opp.id, action.id


def _race_app(engine):
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    app = create_app()

    def _override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app, TestingSession


@pytest.fixture()
def race_db(tmp_path):
    """File-backed DB: request threads get genuinely independent connections
    (a StaticPool in-memory DB would serialize on one shared connection)."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'approve_race.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    @sa.event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_double_approve_race_yields_one_approved(race_db):
    app, TestingSession = _race_app(race_db)
    seed = TestingSession()
    opp_id, action_id = _seed_pending_approval(seed)
    seed.close()

    results: list[tuple[int, dict]] = []
    barrier = threading.Barrier(2)
    winner_done = threading.Event()

    def _approve(tag: int, *, loser: bool) -> None:
        # One TestClient per thread: httpx clients are not thread-shared.
        with TestClient(app) as c:
            barrier.wait(timeout=15)
            if loser:
                # The interleaving FOR UPDATE enforces on Postgres: the loser
                # only reads after the winner's commit.
                winner_done.wait(timeout=60)
            r = c.post(
                f"/api/v1/recovery/{opp_id}/approve",
                json={"actor": f"human:approver{tag}"},
                headers=API_KEY,
            )
            results.append((r.status_code, r.json()))
            if not loser:
                winner_done.set()

    threads = [
        threading.Thread(target=_approve, args=(0,), kwargs={"loser": False}),
        threading.Thread(target=_approve, args=(1,), kwargs={"loser": True}),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    for t in threads:
        assert not t.is_alive(), "approve request thread hung"

    assert len(results) == 2
    codes = sorted(code for code, _ in results)
    assert codes == [200, 409], f"one winner, one clean refusal: {results}"
    loser_body = next(body for code, body in results if code == 409)
    assert "no action awaiting approval" in loser_body["error"]["message"]

    check = TestingSession()
    try:
        action = check.get(models.RecoveryAction, action_id)
        assert action.status is RecoveryStatus.APPROVED
        assert action.approved_by == "human:approver0"  # the winner's stamp
        approved_rows = list(
            check.scalars(
                sa.select(models.AuditLog).where(
                    models.AuditLog.entity_type == "recovery_action",
                    models.AuditLog.entity_id == action_id,
                    models.AuditLog.action == "recovery.action.approved",
                )
            )
        )
        assert len(approved_rows) == 1, (
            f"double approve leaked a second APPROVED transition: {approved_rows}"
        )
    finally:
        check.close()


def test_sequential_double_approve_is_refused(race_db):
    """The same guard without threads: an approve that lands after another
    approve committed is a 409, never a second APPROVED stamp."""
    app, TestingSession = _race_app(race_db)
    seed = TestingSession()
    opp_id, action_id = _seed_pending_approval(seed)
    seed.close()

    with TestClient(app) as c:
        first = c.post(
            f"/api/v1/recovery/{opp_id}/approve",
            json={"actor": "human:first"},
            headers=API_KEY,
        )
        second = c.post(
            f"/api/v1/recovery/{opp_id}/approve",
            json={"actor": "human:second"},
            headers=API_KEY,
        )
    assert first.status_code == 200
    assert first.json()["status"] == "APPROVED"
    assert second.status_code == 409

    check = TestingSession()
    try:
        action = check.get(models.RecoveryAction, action_id)
        assert action.approved_by == "human:first"
    finally:
        check.close()

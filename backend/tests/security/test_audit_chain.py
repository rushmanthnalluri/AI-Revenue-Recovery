"""Hash-chained audit trail — tamper-evidence for the append-only log
(docs/product-strategy.md P2 item 11; see docs/security-testing.md).

Chaining is transparent: the session-level `before_flush` hook in
app.models.system stamps previous_hash/entry_hash on every new AuditLog row,
so every ORM writer — app.services.policy.audit.record, the direct
AuditLog(...) constructions in webhook/executor paths, plain test seeds —
chains with zero per-writer code. `verify_chain` (and GET
/api/v1/audit/verify) replays the chain and localizes tampering.

Core `insert()` bypasses the ORM hook entirely — used below to build honest
pre-chain legacy rows (NULL hashes), exactly what the pre-chain production
table looks like.
"""

from datetime import timedelta
from types import SimpleNamespace

import sqlalchemy as sa

from app.db import utcnow
from app.models import AuditLog
from app.models.system import compute_entry_hash
from app.services.audit import verify_chain
from app.services.policy import audit


def _ts(i: int):
    # strictly increasing and just ahead of real time, so audit.record rows
    # (stamped internally with utcnow()) stay OLDER than fixture-stamped rows
    # — the chain follows insertion order only while timestamps are monotonic
    # across flushes (single-node assumption, see app.models.system).
    return utcnow() + timedelta(seconds=30 * (i + 1))


def _direct(db_session, i: int, **kw) -> AuditLog:
    """Direct ORM construction — the webhook_handlers/executor pattern."""
    row = AuditLog(
        entity_type=kw.pop("entity_type", "recovery_action"),
        entity_id=kw.pop("entity_id", f"act_{i}"),
        actor=kw.pop("actor", "system:webhook"),
        action=kw.pop("action", "verify_recovered"),
        details=kw.pop("details", {"seq": i}),
        created_at=kw.pop("created_at", _ts(i)),
        **kw,
    )
    db_session.add(row)
    return row


def _legacy(db_session, i: int, row_id: str) -> None:
    """Pre-chain row: Core insert bypasses the ORM hook -> NULL hashes.
    Backdated so legacy rows always precede chained rows in walk order."""
    db_session.execute(
        sa.insert(AuditLog).values(
            id=row_id,
            created_at=utcnow() - timedelta(minutes=10) + timedelta(seconds=i),
            entity_type="incident",
            entity_id=f"inc_{i}",
            actor="agent:detection",
            action="incident.created",
            details={"seq": i},
            environment="research",
        )
    )


def _chain(db_session):
    """Four rows via three writer patterns, committed; oldest-first."""
    r1 = audit.record(  # helper writer (flushes per row)
        db_session,
        actor="human:ops",
        action="recovery.approve",
        entity_type="recovery_action",
        entity_id="act_1",
        details={"seq": 1},
        request_id="req-1",
    )
    r2 = audit.record(
        db_session,
        actor="human:ops",
        action="recovery.execute",
        entity_type="recovery_action",
        entity_id="act_1",
        details={"seq": 2},
    )
    db_session.commit()
    # webhook-style writers: direct constructors flushed together at commit,
    # mixed environments — the chain is environment-agnostic by design
    r3 = _direct(db_session, 3, environment="real_test")
    r4 = _direct(db_session, 4, entity_type="incident", entity_id="inc_9", action="incident.created")
    db_session.commit()
    return [r1, r2, r3, r4]


def test_chains_across_mixed_writers(db_session):
    rows = _chain(db_session)
    assert rows[0].previous_hash is None  # genesis
    for prev, cur in zip(rows, rows[1:]):
        assert cur.previous_hash == prev.entry_hash
    for row in rows:
        assert row.entry_hash and len(row.entry_hash) == 64
    report = verify_chain(db_session)
    assert report.valid is True
    assert report.checked == 4
    assert report.chained == 4
    assert report.legacy == 0
    assert report.first_bad_id is None


def test_entry_hash_deterministic_across_reload(db_session):
    """Digest recomputed from a freshly loaded row equals the stored digest
    (canonicalization survives the SQLite write/read round-trip)."""
    rows = _chain(db_session)
    stored = [r.entry_hash for r in rows]
    db_session.expire_all()
    fresh = db_session.scalars(
        sa.select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    ).all()
    assert [compute_entry_hash(r) for r in fresh] == stored


def test_tampered_details_detected_at_the_tampered_row(db_session):
    rows = _chain(db_session)
    target = rows[2]
    db_session.execute(
        sa.update(AuditLog).where(AuditLog.id == target.id).values(details={"seq": 999})
    )
    db_session.commit()
    db_session.expire_all()  # verify reads what the DB holds, not the identity map
    report = verify_chain(db_session)
    assert report.valid is False
    assert report.first_bad_id == target.id
    assert report.checked == 3  # genesis + one good row + the bad row


def test_recomputed_hash_without_cascade_detected_at_successor(db_session):
    """Attacker who knows the scheme edits details AND recomputes the row's
    own entry_hash (but cannot fix successors): the row itself verifies, the
    link break surfaces at the NEXT row."""
    rows = _chain(db_session)
    target, successor = rows[1], rows[2]
    new_details = {"seq": 999}
    forged = compute_entry_hash(
        SimpleNamespace(
            id=target.id,
            created_at=target.created_at,
            actor=target.actor,
            action=target.action,
            entity_type=target.entity_type,
            entity_id=target.entity_id,
            details=new_details,
            previous_hash=target.previous_hash,
        )
    )
    db_session.execute(
        sa.update(AuditLog)
        .where(AuditLog.id == target.id)
        .values(details=new_details, entry_hash=forged)
    )
    db_session.commit()
    db_session.expire_all()
    report = verify_chain(db_session)
    assert report.valid is False
    assert report.first_bad_id == successor.id


def test_deleted_middle_row_detected_at_successor(db_session):
    rows = _chain(db_session)
    victim, successor = rows[1], rows[2]
    db_session.execute(sa.delete(AuditLog).where(AuditLog.id == victim.id))
    db_session.commit()
    db_session.expire_all()
    report = verify_chain(db_session)
    assert report.valid is False
    assert report.first_bad_id == successor.id


def test_legacy_null_hash_rows_verify_as_legacy_valid(db_session):
    _legacy(db_session, 0, "aud_legacy_0")
    _legacy(db_session, 1, "aud_legacy_1")
    db_session.commit()
    report = verify_chain(db_session)
    assert report.valid is True
    assert report.checked == 2
    assert report.legacy == 2
    assert report.chained == 0

    # rows written after the chain ships still verify, legacy prefix intact
    rows = _chain(db_session)
    assert rows[0].previous_hash is None  # genesis skips NULL-hashed legacy rows
    db_session.expire_all()
    report = verify_chain(db_session)
    assert report.valid is True
    assert report.checked == 6
    assert report.legacy == 2
    assert report.chained == 4


def test_unhashed_row_after_genesis_is_flagged(db_session):
    """A NULL-hash row appearing AFTER the chain started bypassed the ORM hook
    (raw SQL) or had its hash stripped — the chain cannot vouch for it."""
    _chain(db_session)
    db_session.execute(
        sa.insert(AuditLog).values(
            id="aud_smuggled",
            created_at=utcnow() + timedelta(minutes=10),
            entity_type="incident",
            entity_id="inc_x",
            actor="agent:detection",
            action="incident.created",
            details={},
            environment="research",
        )
    )
    db_session.commit()
    db_session.expire_all()
    report = verify_chain(db_session)
    assert report.valid is False
    assert report.first_bad_id == "aud_smuggled"


def test_verify_endpoint_empty_db(client):
    r = client.get("/api/v1/audit/verify")  # GET: open-read posture, no API key
    assert r.status_code == 200
    assert r.json() == {
        "valid": True,
        "checked": 0,
        "chained": 0,
        "legacy": 0,
        "first_bad_id": None,
    }


def test_verify_endpoint_clean_chain(client, db_session):
    rows = _chain(db_session)
    r = client.get("/api/v1/audit/verify")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"valid", "checked", "chained", "legacy", "first_bad_id"}
    assert body["valid"] is True
    assert body["checked"] == len(rows)
    assert body["chained"] == len(rows)
    assert body["first_bad_id"] is None


def test_verify_endpoint_reports_tamper(client, db_session):
    rows = _chain(db_session)
    target = rows[2]
    db_session.execute(
        sa.update(AuditLog).where(AuditLog.id == target.id).values(details={"seq": 999})
    )
    db_session.commit()
    db_session.expire_all()
    r = client.get("/api/v1/audit/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["first_bad_id"] == target.id

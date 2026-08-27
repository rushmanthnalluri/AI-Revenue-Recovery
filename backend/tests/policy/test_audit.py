"""The generic audit helper: append-only rows, request-id propagation, and
flush-without-commit transaction semantics."""

from datetime import datetime, timezone

import sqlalchemy as sa

import app.models as models
from app.logging import request_id_ctx
from app.services.policy import audit


class TestRecord:
    def test_writes_a_complete_row(self, db_session):
        entry = audit.record(
            db_session,
            actor="human:ops@example.com",
            action="recovery.approve",
            entity_type="recovery_action",
            entity_id="act_123",
            details={"note": "looks safe"},
            request_id="req-9",
        )
        assert entry.id.startswith("aud_")
        rows = db_session.scalars(sa.select(models.AuditLog)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == "human:ops@example.com"
        assert row.action == "recovery.approve"
        assert row.entity_type == "recovery_action"
        assert row.entity_id == "act_123"
        assert row.details == {"note": "looks safe"}
        assert row.request_id == "req-9"
        assert row.created_at.tzinfo is not None

    def test_defaults(self, db_session):
        audit.record(
            db_session,
            actor="agent:strategist",
            action="recovery.propose",
            entity_type="recovery_action",
            entity_id="act_1",
        )
        row = db_session.scalars(sa.select(models.AuditLog)).one()
        assert row.details == {}
        assert row.request_id is None

    def test_request_id_falls_back_to_contextvar(self, db_session):
        token = request_id_ctx.set("req-from-middleware")
        try:
            audit.record(
                db_session,
                actor="system",
                action="incident.open",
                entity_type="incident",
                entity_id="inc_1",
            )
        finally:
            request_id_ctx.reset(token)
        row = db_session.scalars(sa.select(models.AuditLog)).one()
        assert row.request_id == "req-from-middleware"

    def test_details_are_coerced_to_json_safe(self, db_session):
        when = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        audit.record(
            db_session,
            actor="system",
            action="demo",
            entity_type="demo",
            entity_id="d_1",
            details={"when": when},
        )
        row = db_session.scalars(sa.select(models.AuditLog)).one()
        assert isinstance(row.details["when"], str)
        assert "2026-08-26" in row.details["when"]

    def test_oversized_fields_are_truncated_not_rejected(self, db_session):
        audit.record(
            db_session,
            actor="x" * 500,
            action="demo",
            entity_type="demo",
            entity_id="d_1",
        )
        row = db_session.scalars(sa.select(models.AuditLog)).one()
        assert len(row.actor) == 128

    def test_rows_are_appended(self, db_session):
        for i in range(3):
            audit.record(
                db_session,
                actor="system",
                action=f"step.{i}",
                entity_type="demo",
                entity_id="d_1",
            )
        assert len(db_session.scalars(sa.select(models.AuditLog)).all()) == 3

    def test_flushes_but_never_commits(self, db_session):
        """The caller owns the transaction boundary."""
        audit.record(
            db_session,
            actor="system",
            action="demo",
            entity_type="demo",
            entity_id="d_1",
        )
        db_session.rollback()
        assert db_session.scalars(sa.select(models.AuditLog)).all() == []

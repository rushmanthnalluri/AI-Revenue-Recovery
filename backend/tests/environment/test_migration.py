"""Environment isolation (g): the b4e7a1c2d305 migration upgrades, backfills
'research', and downgrades cleanly on a scratch SQLite file (mirrors the
provenance migration test's pattern)."""

from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.config import settings

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
PREV_REVISION = "f3a9c1e7b204"  # provenance columns (pre-environment)

_ENV_TABLES = (
    "incidents",
    "incident_evidence",
    "recovery_opportunities",
    "recovery_actions",
    "diagnoses",
    "agent_reports",
    "audit_logs",
)


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(sa.text(f"PRAGMA table_info({table})"))}


def test_environment_migration_upgrade_backfill_downgrade(tmp_path, monkeypatch):
    db_file = tmp_path / "env_migration.db"
    url = f"sqlite:///{db_file.as_posix()}"
    # alembic env.py reads the URL from app settings — point it at scratch.
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    cfg = Config(str(ALEMBIC_INI))

    # Pre-environment schema, then one legacy derived row via raw SQL (the
    # current ORM already knows the new column).
    command.upgrade(cfg, PREV_REVISION)
    engine = sa.create_engine(url)
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO incidents (id, title, status, severity, metric,"
                " detection_method, detected_at, affected_payments_count,"
                " revenue_at_risk_paise, currency, meta, created_at, updated_at)"
                " VALUES ('inc_legacy', 'Legacy incident', 'OPEN', 'MEDIUM',"
                " 'payment_success_rate', 'zscore', :n, 0, 0, 'INR', '{}', :n, :n)"
            ),
            {"n": now},
        )

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        # backfill: the legacy derived row is honestly research-stamped
        env = conn.execute(
            sa.text("SELECT environment FROM incidents WHERE id = 'inc_legacy'")
        ).scalar_one()
        assert env == "research"
        for table in _ENV_TABLES:
            assert "environment" in _columns(conn, table), table
        # new tables with the exact contract the sync-service wave builds on
        sync_cols = _columns(conn, "sync_runs")
        assert {
            "id",
            "started_at",
            "finished_at",
            "status",
            "entity_counts",
            "error",
            "actor",
            "request_id",
            "created_at",
        } <= sync_cols
        state_cols = _columns(conn, "connection_state")
        assert {
            "id",
            "sync_enabled",
            "last_sync_at",
            "last_webhook_at",
            "last_sync_status",
            "updated_at",
        } <= state_cols

    command.downgrade(cfg, PREV_REVISION)
    with engine.connect() as conn:
        for table in _ENV_TABLES:
            assert "environment" not in _columns(conn, table), table
        names = {
            r[0]
            for r in conn.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        }
        assert "sync_runs" not in names
        assert "connection_state" not in names
        # data survived the downgrade
        assert (
            conn.execute(sa.text("SELECT count(*) FROM incidents")).scalar_one() == 1
        )

    # Re-upgrade is clean (downgrade genuinely reversed the schema).
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        env = conn.execute(
            sa.text("SELECT environment FROM incidents WHERE id = 'inc_legacy'")
        ).scalar_one()
        assert env == "research"
    engine.dispose()

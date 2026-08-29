"""environment core: environment columns + sync_runs/connection_state

Revision ID: b4e7a1c2d305
Revises: f3a9c1e7b204
Create Date: 2026-08-29 07:30:00.000000

Adds the REAL MERCHANT vs RESEARCH boundary (docs/data-provenance.md,
"Environment model"):

- ``environment`` (VARCHAR(16) NOT NULL, server default 'research', indexed)
  on the derived tables: incidents, incident_evidence,
  recovery_opportunities, recovery_actions, diagnoses, agent_reports,
  audit_logs. Commerce tables keep NO environment column — their environment
  is derived from source_type ('razorpay_test'/'razorpay_live' -> real_test,
  'simulator' -> research).
- Backfill: every existing row is simulator-derived (real ingestion does not
  exist yet), so 'research' is the honest stamp; the server default performs
  it and the explicit UPDATEs below document it.
- New table ``sync_runs``: one row per real-ingestion sync pass (the sync
  service wave builds against exactly this shape).
- New table ``connection_state``: singleton ('merchant') connection cursor —
  sync toggle + last sync/webhook timestamps.

Written explicitly (no autogenerate); batch mode so SQLite and Postgres emit
equivalent results (SQLite recreates the tables, Postgres plain ALTERs).
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

import app.db  # noqa: F401  (TZDateTime custom type used below)


revision: str = 'b4e7a1c2d305'
down_revision: str | None = 'f3a9c1e7b204'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Derived tables gaining the environment column. Commerce tables deliberately
# do NOT: their environment is derived from source_type.
_ENV_TABLES = (
    'incidents',
    'incident_evidence',
    'recovery_opportunities',
    'recovery_actions',
    'diagnoses',
    'agent_reports',
    'audit_logs',
)


def upgrade() -> None:
    for table in _ENV_TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column('environment', sa.String(length=16), nullable=False,
                          server_default='research'),
            )
            batch_op.create_index(
                op.f(f'ix_{table}_environment'), ['environment'], unique=False
            )
        # Honest backfill: every pre-existing row is simulator-derived.
        op.execute(f"UPDATE {table} SET environment = 'research'")

    op.create_table(
        'sync_runs',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('started_at', app.db.TZDateTime(timezone=True), nullable=False),
        sa.Column('finished_at', app.db.TZDateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='running'),
        sa.Column('entity_counts', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('actor', sa.String(length=128), nullable=False),
        sa.Column('request_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', app.db.TZDateTime(timezone=True), nullable=False),
    )

    op.create_table(
        'connection_state',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('sync_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('last_sync_at', app.db.TZDateTime(timezone=True), nullable=True),
        sa.Column('last_webhook_at', app.db.TZDateTime(timezone=True), nullable=True),
        sa.Column('last_sync_status', sa.String(length=16), nullable=True),
        sa.Column('updated_at', app.db.TZDateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('connection_state')
    op.drop_table('sync_runs')

    for table in _ENV_TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_index(op.f(f'ix_{table}_environment'))
            batch_op.drop_column('environment')

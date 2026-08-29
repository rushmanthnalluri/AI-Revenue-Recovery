"""provenance columns on commerce tables

Revision ID: f3a9c1e7b204
Revises: 77c0efef3d84
Create Date: 2026-08-28 14:13:00.000000

Adds source_type / source_system / external_id / ingested_at to merchants,
customers, orders, payments, payment_events, subscriptions (see
docs/data-provenance.md). Written explicitly (no autogenerate).

Backfill honesty rules:
- source_type stays at the server default 'simulator' for every existing
  row: the simulator engine is the only commerce-row writer that existed
  before this migration.
- source_system is set to 'pulserecover-simulator' on the five
  single-writer tables; payment_events keeps NULL (mixed writers — its
  `source` column already carries simulator/webhook/poller/seed detail).
- external_id mirrors the existing gateway_* id where one exists.
- ingested_at defaults to the migration timestamp (the true ingestion time
  of legacy rows was never recorded); new rows get utcnow from the ORM
  default (SQLite forbids non-constant defaults in ALTER TABLE ADD COLUMN,
  so the NOT NULL is enforced by a batch recreate after the backfill).
- payments gains UNIQUE (source_type, external_id) so one upstream payment
  id can never be stored twice under the same source. Backfilled values
  are the already-unique gateway_payment_id, so the constraint cannot
  collide; NULLs stay distinct on both SQLite and Postgres.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

import app.db  # noqa: F401  (TZDateTime custom type used below)


revision: str = 'f3a9c1e7b204'
down_revision: str | None = '77c0efef3d84'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = ('merchants', 'customers', 'orders', 'payments', 'payment_events', 'subscriptions')
# table -> gateway-id column mirrored into external_id (None: no upstream id)
_EXTERNAL_ID_FROM = {
    'merchants': 'gateway_account_id',
    'customers': 'gateway_customer_id',
    'orders': 'gateway_order_id',
    'payments': 'gateway_payment_id',
    'payment_events': None,
    'subscriptions': 'gateway_subscription_id',
}
# payment_events has mixed writers; do not guess a source_system for old rows.
_SOURCE_SYSTEM_BACKFILL = ('merchants', 'customers', 'orders', 'payments', 'subscriptions')


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column('source_type', sa.String(length=32), nullable=False,
                      server_default='simulator'),
        )
        op.add_column(table, sa.Column('source_system', sa.String(length=64), nullable=True))
        op.add_column(table, sa.Column('external_id', sa.String(length=64), nullable=True))
        # nullable first: SQLite rejects CURRENT_TIMESTAMP in ALTER TABLE ADD
        # COLUMN; backfill below, then NOT NULL via batch recreate.
        op.add_column(
            table,
            sa.Column('ingested_at', app.db.TZDateTime(timezone=True), nullable=True),
        )
        op.create_index(op.f(f'ix_{table}_source_type'), table, ['source_type'], unique=False)
        op.execute(f"UPDATE {table} SET ingested_at = CURRENT_TIMESTAMP")

    for table, gw_col in _EXTERNAL_ID_FROM.items():
        if gw_col is not None:
            op.execute(
                f"UPDATE {table} SET external_id = {gw_col} WHERE {gw_col} IS NOT NULL"
            )
    for table in _SOURCE_SYSTEM_BACKFILL:
        op.execute(f"UPDATE {table} SET source_system = 'pulserecover-simulator'")

    # batch mode: portable ADD/DROP CONSTRAINT + SET NOT NULL (SQLite
    # recreates the table; Postgres emits plain ALTERs).
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column('ingested_at', nullable=False)
            if table == 'payments':
                batch_op.create_unique_constraint(
                    'uq_payments_source_external', ['source_type', 'external_id']
                )


def downgrade() -> None:
    with op.batch_alter_table('payments') as batch_op:
        batch_op.drop_constraint('uq_payments_source_external', type_='unique')

    for table in _TABLES:
        op.drop_index(op.f(f'ix_{table}_source_type'), table_name=table)
        op.drop_column(table, 'ingested_at')
        op.drop_column(table, 'external_id')
        op.drop_column(table, 'source_system')
        op.drop_column(table, 'source_type')

"""durable recovery outcome observations

Revision ID: c6f7a8b9d012
Revises: a83af82e8438
Create Date: 2026-09-05 12:00:00.000000

Adds one immutable, environment-scoped observation per action/outcome state.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

import app.db  # noqa: F401

revision: str = "c6f7a8b9d012"
down_revision: str | None = "a83af82e8438"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recovery_outcome_observations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=False),
        sa.Column("opportunity_id", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("observed_status", sa.String(length=32), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False, server_default="research"),
        sa.Column("decision_at", app.db.TZDateTime(timezone=True), nullable=False),
        sa.Column("observed_at", app.db.TZDateTime(timezone=True), nullable=False),
        sa.Column("policy_decision_id", sa.String(length=64), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("gateway_request_id", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", app.db.TZDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["action_id"], ["recovery_actions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["recovery_opportunities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("action_id", "observed_status", name="uq_recovery_outcome_action_status"),
    )
    op.create_index(
        "ix_recovery_outcome_environment_observed_at",
        "recovery_outcome_observations",
        ["environment", "observed_at"],
        unique=False,
    )
    op.create_index(
        "ix_recovery_outcome_action_id",
        "recovery_outcome_observations",
        ["action_id"],
        unique=False,
    )
    op.create_index(
        "ix_recovery_outcome_policy_decision_id",
        "recovery_outcome_observations",
        ["policy_decision_id"],
        unique=False,
    )
    op.create_index(
        "ix_recovery_outcome_gateway_request_id",
        "recovery_outcome_observations",
        ["gateway_request_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_recovery_outcome_gateway_request_id", table_name="recovery_outcome_observations")
    op.drop_index("ix_recovery_outcome_policy_decision_id", table_name="recovery_outcome_observations")
    op.drop_index("ix_recovery_outcome_action_id", table_name="recovery_outcome_observations")
    op.drop_index("ix_recovery_outcome_environment_observed_at", table_name="recovery_outcome_observations")
    op.drop_table("recovery_outcome_observations")

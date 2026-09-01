"""Hot-path index spec on Base.metadata — the contract the wave-2 alembic
migration must satisfy (the model layer declares indexes; migrations are
generated centrally, never per-change).

Covered query paths (see the __table_args__ comments in app/models/commerce.py):
- payments.created_at: range-filtered and/or sorted by the payments list
  (app/api/v1/payments.py), the revenue engine's baseline/window scans
  (app/services/revenue/engine.py), and the opportunity builder's failed/stuck
  payment selection (app/services/recovery/builder.py).
- orders.created_at: range-filtered and sorted by the builder's
  abandoned-checkout scan.
- payments/orders.source_type: filtered by every environment-scoped query;
  already indexed via ProvenanceMixin (pinned here against regression).
"""

import sqlalchemy as sa

from app.db import Base
import app.models  # noqa: F401  (register all tables on Base.metadata)


def _index(table: str, name: str) -> sa.Index:
    try:
        return next(ix for ix in Base.metadata.tables[table].indexes if ix.name == name)
    except StopIteration:
        raise AssertionError(f"missing index {name} on {table}") from None


def test_payments_created_at_index():
    ix = _index("payments", "ix_payments_created_at")
    assert [c.name for c in ix.columns] == ["created_at"]
    assert not ix.unique


def test_orders_created_at_index():
    ix = _index("orders", "ix_orders_created_at")
    assert [c.name for c in ix.columns] == ["created_at"]
    assert not ix.unique


def test_environment_scoping_indexes_already_present():
    # source_type is the environment filter on every commerce hot path; the
    # ProvenanceMixin index must never be dropped.
    for table in ("payments", "orders"):
        ix = _index(table, f"ix_{table}_source_type")
        assert [c.name for c in ix.columns] == ["source_type"]


def test_payments_source_external_dedup_constraint_intact():
    constraints = Base.metadata.tables["payments"].constraints
    uq = next(
        (c for c in constraints if c.name == "uq_payments_source_external"),
        None,
    )
    assert uq is not None
    assert [c.name for c in uq.columns] == ["source_type", "external_id"]


def test_sync_runs_server_defaults_match_migration():
    # ORM must mirror migration b4e7a1c2d305's server defaults so raw-SQL
    # inserts (and autogenerate diffs) see the same defaults as ORM ones.
    sync_runs = Base.metadata.tables["sync_runs"]
    status_default = sync_runs.c.status.server_default
    counts_default = sync_runs.c.entity_counts.server_default
    assert status_default is not None and str(status_default.arg) == "running"
    assert counts_default is not None and str(counts_default.arg) == "{}"


def test_metadata_ddl_builds_on_sqlite():
    # The declared indexes + JSON server_default must render valid DDL.
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with engine.connect() as conn:
            names = {
                row[1]
                for row in conn.execute(sa.text("PRAGMA index_list('payments')"))
            } | {row[1] for row in conn.execute(sa.text("PRAGMA index_list('orders')"))}
    finally:
        engine.dispose()
    assert "ix_payments_created_at" in names
    assert "ix_orders_created_at" in names

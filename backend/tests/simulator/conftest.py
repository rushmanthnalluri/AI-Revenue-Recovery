"""Shared fixtures/helpers for simulator tests: fresh in-memory sessions and
small-scale configs. The volume test separately exercises the full default
scale (60k+ payment_events)."""

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.simulator import SimulatorConfig, run_simulation

# Fixed window anchor so timestamps (and incident windows) are reproducible
# regardless of the wall clock.
FIXED_END = datetime(2026, 8, 27, tzinfo=timezone.utc)


def fresh_session() -> Session:
    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from app.db import enable_sqlite_fk

    enable_sqlite_fk(engine)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def small_config(**overrides) -> SimulatorConfig:
    cfg = dict(
        seed=7,
        days=10,
        target_events=12_000,
        customers=600,
        scenario="test",
        end_date=FIXED_END,
    )
    cfg.update(overrides)
    return SimulatorConfig(**cfg)


@pytest.fixture()
def sim_session():
    session = fresh_session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def run_sim(sim_session):
    def _run(**overrides):
        return run_simulation(small_config(**overrides), sim_session)

    return _run

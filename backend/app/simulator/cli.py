"""CLI plumbing shared by ``python -m app.simulator``, scripts/seed.py and
scripts/simulate.py.

Owns: argument parsing, engine/session construction (with a --database-url
override so seeding never has to touch app.db), SQLite speed pragmas,
idempotent skip / --force regeneration, and the JSON summary printed at the
end.
"""

import argparse
import json
import sys
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base
from app.models import SimulatorRun  # noqa: F401  (register table)
from app.simulator.config import (
    IncidentKind,
    SCENARIOS,
    SimulatorConfig,
    default_incidents,
)
from app.simulator.engine import (
    SimResult,
    delete_simulator_run,
    run_simulation,
)


def build_parser(prog: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog,
        description="Seed the PulseRecover synthetic payment environment.",
    )
    p.add_argument("--events", type=int, default=SimulatorConfig.target_events,
                   help="target payment_events count (floor; default 65000)")
    p.add_argument("--days", type=int, default=SimulatorConfig.days,
                   help="length of the simulated window in days (default 30)")
    p.add_argument("--seed", type=int, default=SimulatorConfig.seed,
                   help="RNG seed — same seed + config -> identical dataset")
    p.add_argument("--customers", type=int, default=SimulatorConfig.customers,
                   help="number of customers (default 3000)")
    p.add_argument(
        "--incidents", default="default",
        help="'default' | 'none' | comma-separated incident kinds "
             f"({', '.join(k.value for k in IncidentKind)})",
    )
    p.add_argument("--scenario", default="standard",
                   help="scenario label stored on the simulator_runs row")
    p.add_argument("--database-url", default=None,
                   help="override DATABASE_URL (default: app settings)")
    p.add_argument("--force", action="store_true",
                   help="delete any existing run with the same id and reseed")
    return p


def parse_incidents(value: str) -> tuple:
    value = (value or "default").strip().lower()
    if value == "default":
        return default_incidents()
    if value in ("none", "off", ""):
        return ()
    wanted = {v.strip() for v in value.split(",") if v.strip()}
    known = {k.value for k in IncidentKind}
    unknown = wanted - known
    if unknown:
        raise SystemExit(
            f"unknown incident kind(s): {sorted(unknown)}; known: {sorted(known)}"
        )
    return tuple(i for i in default_incidents() if i.kind.value in wanted)


def config_from_args(args: argparse.Namespace) -> SimulatorConfig:
    return SimulatorConfig(
        seed=args.seed,
        days=args.days,
        target_events=args.events,
        customers=args.customers,
        scenario=args.scenario,
        incidents=parse_incidents(args.incidents),
    )


def make_session(database_url: str | None):
    """Own engine (never app.db's) so a scratch --database-url works and
    SQLite speed pragmas stay local to seeding."""
    url = database_url or settings.DATABASE_URL
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = sa.create_engine(url, connect_args=connect_args)

    if url.startswith("sqlite"):
        @sa.event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA temp_store=MEMORY")
            cur.execute("PRAGMA cache_size=-64000")
            cur.close()

    Base.metadata.create_all(engine)  # no-op when alembic already ran
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def run_idempotent(session, config: SimulatorConfig, force: bool) -> SimResult:
    """Skip when an identical completed run exists; --force deletes it first.
    Idempotency key = deterministic run id (seed + config hash)."""
    existing = session.get(SimulatorRun, config.run_id)
    if existing is not None:
        if not force and existing.status == "completed":
            stats = dict(existing.stats or {})
            stats["skipped"] = True
            stats["note"] = "identical run already seeded; use --force to regenerate"
            return SimResult(config.run_id, stats, skipped=True)
        delete_simulator_run(session, config.run_id)
    return run_simulation(config, session)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser(prog="seed").parse_args(argv)
    config = config_from_args(args)
    session = make_session(args.database_url)
    try:
        result = run_idempotent(session, config, force=args.force)
    finally:
        session.close()
    summary = {"run_id": result.run_id, "scenario": config.scenario, **result.stats}
    print(json.dumps(summary, indent=2, default=str))
    return 0


def scenario_main(argv: Sequence[str] | None = None) -> int:
    """scripts/simulate.py entry: run a named scenario preset."""
    p = argparse.ArgumentParser(
        prog="simulate",
        description="Run a named simulator scenario preset.",
    )
    p.add_argument("--scenario", default="standard",
                   choices=sorted(SCENARIOS), help="preset name")
    p.add_argument("--list", action="store_true", help="list presets and exit")
    p.add_argument("--database-url", default=None)
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)
    if args.list:
        from app.simulator import list_scenarios

        print(json.dumps(list_scenarios(), indent=2))
        return 0
    factory = SCENARIOS[args.scenario][1]
    config: SimulatorConfig = factory()  # type: ignore[operator]
    session = make_session(args.database_url)
    try:
        result = run_idempotent(session, config, force=args.force)
    finally:
        session.close()
    summary = {"run_id": result.run_id, "scenario": config.scenario, **result.stats}
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

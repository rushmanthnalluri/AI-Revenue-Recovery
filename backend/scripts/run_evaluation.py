"""Run a baseline-vs-PulseRecover evaluation from the command line.

Run from backend/:

    .venv/Scripts/python scripts/run_evaluation.py --scenario standard
    .venv/Scripts/python scripts/run_evaluation.py --scenario upi_outage_demo --days 10 --events 20000

Mirrors POST /api/v1/evaluation/run: both arms run in isolated scratch
SQLite databases (seeded by the real simulator), and one evaluation_runs row
(+ one experiments row) is persisted to the target database. Full-preset
scenarios take a few minutes; use --days/--events to shrink for a smoke run.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from app.services.evaluation import EvaluationRunner
from app.simulator.cli import make_session


def main() -> int:
    p = argparse.ArgumentParser(
        prog="run_evaluation",
        description="Baseline-vs-PulseRecover evaluation over a simulator scenario.",
    )
    p.add_argument("--scenario", default="standard", help="simulator scenario preset")
    p.add_argument("--name", default=None, help="run name (default: cli:<scenario>)")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--events", type=int, default=None)
    p.add_argument("--customers", type=int, default=None)
    p.add_argument("--holdout-fraction", type=float, default=None,
                   help="share of customers randomized into the no-action holdout "
                        "inside the PulseRecover arm (default: 0.10; 0 disables)")
    p.add_argument("--database-url", default=None,
                   help="where to persist the evaluation_runs row (default: app settings)")
    args = p.parse_args()

    session = make_session(args.database_url)
    try:
        run = EvaluationRunner(session).run(
            name=args.name or f"cli:{args.scenario}",
            scenario=args.scenario,
            seed=args.seed,
            days=args.days,
            events=args.events,
            customers=args.customers,
            holdout_fraction=args.holdout_fraction,
        )
    finally:
        session.close()
    print(json.dumps({"run_id": run.id, "status": run.status, "metrics": run.metrics},
                     indent=2, default=str))
    return 0 if run.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())

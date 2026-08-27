"""PulseRecover simulator: deterministic synthetic payment environment.

Generates realistic payment traffic (merchants/customers/orders/payments/
payment_events/subscriptions) with injected, parameterized incident windows,
and records exactly what was injected in ``simulator_ground_truth`` — the
scientific scoring key for detection/diagnosis/recovery evaluation (ADR 0005).

Quick start (from backend/):

    python -m app.simulator --events 65000 --days 30 --seed 42
    python scripts/seed.py --force          # idempotent wrapper
    python scripts/simulate.py --scenario upi_outage_demo

See docs/simulator.md for distributions, assumptions, the incident taxonomy,
and the ground-truth → label mapping.
"""

from app.simulator.config import (
    IncidentKind,
    IncidentSpec,
    SCENARIOS,
    SimulatorConfig,
    default_incidents,
    list_scenarios,
)
from app.simulator.engine import (
    SimResult,
    SimulatorEngine,
    delete_simulator_run,
    run_simulation,
)

__all__ = [
    "IncidentKind",
    "IncidentSpec",
    "SCENARIOS",
    "SimulatorConfig",
    "default_incidents",
    "list_scenarios",
    "SimResult",
    "SimulatorEngine",
    "delete_simulator_run",
    "run_simulation",
]

"""Run a named simulator scenario preset (demo-friendly).

Run from backend/:  python scripts/simulate.py --scenario upi_outage_demo [--force]
                    python scripts/simulate.py --list

Presets live in app.simulator.config.SCENARIOS. Same idempotency semantics as
scripts/seed.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from app.simulator.cli import scenario_main

if __name__ == "__main__":
    sys.exit(scenario_main())

"""Idempotent seed of the PulseRecover simulator dataset.

Run from backend/:  python scripts/seed.py [--events N] [--days N] [--seed N]
                    [--customers N] [--incidents default|none|kind,kind]
                    [--database-url sqlite:///...] [--force]

Idempotency: the run id is derived from (seed, config hash). If a completed
simulator_runs row with that id exists, the seed is a no-op; --force deletes
the existing run's data and regenerates.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from app.simulator.cli import main

if __name__ == "__main__":
    sys.exit(main())

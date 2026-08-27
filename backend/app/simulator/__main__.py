"""`python -m app.simulator` — seed the synthetic payment environment.

Run from backend/:  python -m app.simulator --events 65000 --days 30 --seed 42
"""

import sys

from app.simulator.cli import main

if __name__ == "__main__":
    sys.exit(main())

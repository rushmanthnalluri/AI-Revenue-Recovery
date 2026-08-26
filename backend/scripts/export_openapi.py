"""Export the live OpenAPI spec to contracts/openapi.json at the repo root.

Run from backend/:  python scripts/export_openapi.py
The generated contract is committed so the frontend agent can generate a typed
client without running the backend.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from app.main import create_app

OUT = Path(__file__).resolve().parents[2] / "contracts" / "openapi.json"


def main() -> None:
    app = create_app()
    spec = app.openapi()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(spec['paths'])} paths)")


if __name__ == "__main__":
    main()

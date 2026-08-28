#!/usr/bin/env python
"""Cross-check: multi-anchor records ↔ stored runs ↔ docs/evaluation.md §3d.

Proves the numbers quoted in docs/evaluation.md §3d and in this directory's
analysis.md / aggregate.json are exactly what the stored evaluation_runs rows
say — no transcription drift, no stale tables.

Checks (each printed PASS/FAIL; exit 1 on any FAIL):
  1. every anchor's metrics_<anchor>.json["metrics"] is byte-equal to the
     stored run row's metrics in the multi-anchor database;
  2. aggregate.json equals a fresh recomputation from the per-anchor files;
  3. the table block between the MULTI-ANCHOR-TABLES markers in
     docs/evaluation.md §3d is byte-equal to section_3d_tables.md;
  4. the 2026-08-28 anchor still reproduces canonical_spec.json's expected
     values (recomputed live from the stored row).

Run from the repo root:  backend/.venv/Scripts/python ml/experiments/multi_anchor/cross_check.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR / "scripts"))
sys.path.insert(0, str(BACKEND_DIR))

import run_multi_anchor as rma  # noqa: E402
from app.models import EvaluationRun  # noqa: E402
from app.simulator.cli import make_session  # noqa: E402

START = "<!-- MULTI-ANCHOR-TABLES-START -->"
END = "<!-- MULTI-ANCHOR-TABLES-END -->"


def _load(name: str):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):  # cp1252 consoles vs ✓/— in tables
        sys.stdout.reconfigure(errors="replace")
    failures: list[str] = []
    config = _load("config.json")
    aggregate = _load("aggregate.json")
    anchors = [a for a in config["anchors"] if a in aggregate["anchors_ok"]]

    # -- 1. per-anchor files vs stored run rows ---------------------------
    session = make_session(config["database_url"])
    try:
        for anchor in anchors:
            record = _load(f"metrics_{anchor}.json")
            run_id = config["runs"][anchor]["run_id"]
            row = session.get(EvaluationRun, run_id)
            if row is None:
                failures.append(f"{anchor}: run {run_id} missing from database")
                continue
            same = json.dumps(record["metrics"], sort_keys=True, default=str) == json.dumps(
                row.metrics, sort_keys=True, default=str
            )
            status = "PASS" if same else "FAIL"
            if not same:
                failures.append(f"{anchor}: metrics file != stored row {run_id}")
            print(f"[{status}] {anchor}: metrics file == stored row {run_id}")
    finally:
        session.close()

    # -- 2. aggregate.json vs recomputation from per-anchor files ---------
    heads = [_load(f"metrics_{a}.json")["headline"] for a in anchors]
    recomputed = rma.aggregate(heads)
    drift = []
    for key, entry in recomputed.items():
        stored = aggregate["aggregates"].get(key)
        if stored != entry:
            drift.append(key)
    if drift:
        failures.append(f"aggregate.json drift on: {drift}")
    print(f"[{'FAIL' if drift else 'PASS'}] aggregate.json == recomputation "
          f"from {len(heads)} per-anchor files")

    # -- 3. docs §3d table block vs generated tables ----------------------
    docs = (REPO_ROOT / "docs" / "evaluation.md").read_text(encoding="utf-8")
    tables = (HERE / "section_3d_tables.md").read_text(encoding="utf-8").strip()
    try:
        block = docs.split(START, 1)[1].split(END, 1)[0].strip()
    except IndexError:
        block = None
    if block is None:
        failures.append("docs/evaluation.md: MULTI-ANCHOR-TABLES markers not found")
        print("[FAIL] docs/evaluation.md §3d markers not found")
    else:
        same = block == tables
        if not same:
            failures.append("docs/evaluation.md §3d table block != section_3d_tables.md")
        print(f"[{'PASS' if same else 'FAIL'}] docs/evaluation.md §3d tables "
              f"== section_3d_tables.md")

    # -- 4. canonical anchor still reproduces the spec --------------------
    if "2026-08-28" in anchors:
        head = _load("metrics_2026-08-28.json")["headline"]
        check = rma.canonical_spec_check(head)
        ok = check["result"] == "MATCH"
        if not ok:
            failures.append(f"canonical spec check: {check['mismatches']}")
        print(f"[{'PASS' if ok else 'FAIL'}] 2026-08-28 anchor reproduces "
              f"canonical_spec.json expected values ({check['compared']} compared)")

    print()
    print(rma.render_tables(
        anchors,
        {
            a: {
                "run_id": config["runs"][a]["run_id"],
                "dataset_version": config["runs"][a]["dataset_version"],
                "wall_clock_seconds": _load(f"metrics_{a}.json")["wall_clock_seconds"],
                "headline": _load(f"metrics_{a}.json")["headline"],
                "all_kinds": config["kind_order"],
            }
            for a in anchors
        },
        recomputed,
    ))
    if failures:
        print("\nCROSS-CHECK FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll cross-checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

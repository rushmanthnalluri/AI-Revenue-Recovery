#!/usr/bin/env python
"""Merge prod_frames_v1 + shard2 into prod_frames_v2 (experiment exp04).

v2 exists because the v1 held-out blocks (val 51 / test 53 rows) could not
statistically resolve the deploy/keep decision on the business metric — the
scale-up was decided from v1's *validation* noise, before any v2 test block
was seen, and the candidate set + pre-registered selection rule are
unchanged. v1 rows are reused bit-identically (same build command, recorded
in exp01); shard2 is seeds 5072-5143 (build_shard2.log, config.json here).

Run from the repo root:

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp04_prod_frames_v2/merge_v2.py
"""

import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
V1 = REPO_ROOT / "backend" / "artifacts" / "prod_frames_v1.csv"
SHARD2 = REPO_ROOT / "backend" / "artifacts" / "prod_frames_v2_shard2.csv"
OUT = REPO_ROOT / "backend" / "artifacts" / "prod_frames_v2.csv"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    exp_dir = Path(__file__).resolve().parent
    with V1.open(newline="", encoding="utf-8") as fh:
        v1_rows = list(csv.DictReader(fh))
    with SHARD2.open(newline="", encoding="utf-8") as fh:
        shard2_rows = list(csv.DictReader(fh))
    rows = v1_rows + shard2_rows
    fieldnames = list(v1_rows[0].keys())
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    label_dist = Counter(r["label"] for r in rows)
    summary = {
        "dataset_version": "prod_frames_v2",
        "composition": {
            "prod_frames_v1": {"rows": len(v1_rows), "sha256": _sha256(V1)},
            "prod_frames_v2_shard2": {"rows": len(shard2_rows), "sha256": _sha256(SHARD2)},
        },
        "rows": len(rows),
        "seeds": "5000-5143 (144 simulator seeds; two build shards, one base-end anchor each — anchors in the shards' config.json)",
        "label_distribution": dict(sorted(label_dist.items())),
        "no_fault_share": round(label_dist.get("no_fault", 0) / len(rows), 4),
        "per_scenario_rows": dict(sorted(Counter(r["scenario"] for r in rows).items())),
        "multi_overlap_rows": sum(1 for r in rows if "|" in r["overlapping_entity_ids"]),
        "sha256": _sha256(OUT),
    }
    merge_config = {
        "experiment": "exp04_prod_frames_v2",
        "script": str(Path(__file__).relative_to(REPO_ROOT)),
        "git_sha": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=10,
        ).stdout.strip(),
        "rationale": summary["seeds"],
    }
    (exp_dir / "merge_config.json").write_text(json.dumps(merge_config, indent=2) + "\n", encoding="utf-8")
    (exp_dir / "dataset_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "composition"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

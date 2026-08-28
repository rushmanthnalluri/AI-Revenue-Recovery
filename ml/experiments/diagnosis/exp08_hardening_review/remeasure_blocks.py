#!/usr/bin/env python
"""exp08 hardening review — re-measure the SHIPPED artifact on the recorded blocks.

Verification, not a new campaign: loads the ACTIVE artifact
(``backend/artifacts/diagnosis_active.json`` -> joblib), rebuilds the exact
held-out blocks from the RECORDED dataset CSVs (same ``temporal_split`` call
as exp07/run_v4.py, no dataset rebuild), re-scores with the same
``compute_metrics`` code path, and diffs every number against
``exp07_tight_frame_v4/ship_metrics.json`` (4dp, the ship record's own
convention). The exp06 incumbent LR (still in backend/artifacts/ for
rollback) is re-scored on the same blocks so the pre-registered gate
comparison is re-run end-to-end, not carried over.

Dataset integrity is pinned by sha256 against config_v4b2.json before any
scoring — a silent dataset drift fails the run.

Run from the repo root:

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp08_hardening_review/remeasure_blocks.py
"""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.services.diagnosis.features import FEATURE_NAMES, features_to_vector  # noqa: E402
from app.services.diagnosis.taxonomy import CAUSES  # noqa: E402
from app.services.diagnosis.training import (  # noqa: E402
    compute_metrics,
    load_active_artifact,
    temporal_split,
)

EXP07 = REPO_ROOT / "ml" / "experiments" / "diagnosis" / "exp07_tight_frame_v4"
OUT_DIR = Path(__file__).resolve().parent

# (block key, csv path, recorded sha256 in config_v4b2.json)
SOURCES = {
    "prod_v3_test": BACKEND / "artifacts" / "prod_frames_v3.csv",
    "tight_test": BACKEND / "artifacts" / "tight_frames_v1.csv",
    "span_test": BACKEND / "artifacts" / "sim_features.csv",
}
INCUMBENT_LR = BACKEND / "artifacts" / "diagnosis_logistic_regression_v20260826T234303Z-c5434878.joblib"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _score(est, block: pd.DataFrame, labels: list[str]) -> dict:
    """Byte-for-byte the _score of exp07/run_v4.py."""
    X = np.asarray([features_to_vector(r) for r in block[FEATURE_NAMES].to_dict("records")])
    proba = est.predict_proba(X)
    classes = [str(c) for c in est.classes_]
    y_pred = np.asarray([classes[j] for j in proba.argmax(axis=1)])
    return compute_metrics(block["label"].to_numpy(), y_pred, proba, est.classes_, labels)


def _diff_record(recorded: dict, fresh: dict, path: str = "") -> list[str]:
    """4dp-tolerant diff of every leaf number/string in two metric dicts."""
    problems: list[str] = []
    for key, rv in recorded.items():
        fv = fresh.get(key, "<missing>")
        here = f"{path}.{key}" if path else key
        if isinstance(rv, dict) and isinstance(fv, dict):
            problems += _diff_record(rv, fv, here)
        elif isinstance(rv, list):
            if json.dumps(rv, sort_keys=True) != json.dumps(fv, sort_keys=True):
                problems.append(f"{here}: recorded != fresh")
        elif isinstance(rv, (int, float)) and isinstance(fv, (int, float)):
            if round(float(rv), 4) != round(float(fv), 4):
                problems.append(f"{here}: recorded {rv} != fresh {fv}")
        else:
            if rv != fv:
                problems.append(f"{here}: recorded {rv!r} != fresh {fv!r}")
    return problems


def main() -> int:
    config_v4b2 = json.loads((EXP07 / "config_v4b2.json").read_text(encoding="utf-8"))
    recorded_ship = json.loads((EXP07 / "ship_metrics.json").read_text(encoding="utf-8"))
    recorded = recorded_ship["metrics"]
    recorded_src = config_v4b2["dataset"]["sources"]
    src_key = {
        "prod_v3_test": "prod_detection_window_v3",
        "tight_test": "tight_adhoc_frame_v1",
        "span_test": "exact_span_frame",
    }

    # 1. dataset integrity — the blocks I score must be the blocks exp07 scored
    integrity = {}
    for block_key, csv_path in SOURCES.items():
        digest = _sha256(csv_path)
        want = recorded_src[src_key[block_key]]["sha256"]
        integrity[block_key] = {"csv": csv_path.name, "sha256": digest, "recorded_sha256": want, "match": digest == want}
        if digest != want:
            print(f"FATAL: {csv_path.name} sha256 drifted from config_v4b2.json record")
            return 2

    # 2. rebuild the exact held-out blocks (same code path as run_v4.py)
    labels = list(CAUSES)
    blocks = {}
    for block_key, csv_path in SOURCES.items():
        df = pd.read_csv(csv_path)
        df["window_end"] = pd.to_datetime(df["window_end"], utc=True)
        _, _, test = temporal_split(df, 0.6, 0.2)
        blocks[block_key] = test

    # 3. score the SHIPPED artifact (loaded through the active pointer)
    active = load_active_artifact(BACKEND / "artifacts")
    assert active is not None, "no active artifact"
    est = active["model"]
    fresh_shipped = {k: _score(est, b, labels) for k, b in blocks.items()}

    # 4. re-score the exp06 incumbent LR on the same blocks (gate comparison re-run)
    inc = joblib.load(INCUMBENT_LR)
    inc_est = inc["model"]
    fresh_incumbent = {k: _score(inc_est, b, labels) for k, b in blocks.items()}

    # 5. diff fresh-vs-recorded for the shipped model (4dp convention)
    diffs: dict[str, list[str]] = {}
    for block_key in blocks:
        diffs[block_key] = _diff_record(recorded[block_key], fresh_shipped[block_key])

    # 6. re-evaluate every pre-registered exp07 gate clause with fresh numbers
    ship_prod, inc_prod = fresh_shipped["prod_v3_test"], fresh_incumbent["prod_v3_test"]
    ship_span, inc_span = fresh_shipped["span_test"], fresh_incumbent["span_test"]
    demo = recorded["demo"]  # demo frames not re-computed in this review (scope note in config)
    gate = {
        "prod_safe_strictly_better": {
            "fresh_shipped": ship_prod["business"]["safe_auto_lane_coverage"],
            "fresh_incumbent": inc_prod["business"]["safe_auto_lane_coverage"],
            "pass": ship_prod["business"]["safe_auto_lane_coverage"] > inc_prod["business"]["safe_auto_lane_coverage"],
        },
        "prod_unsafe_materially_lower": {
            "fresh_shipped": ship_prod["business"]["unsafe_coverage"],
            "fresh_incumbent": inc_prod["business"]["unsafe_coverage"],
            "pass": ship_prod["business"]["unsafe_coverage"]
            <= round(inc_prod["business"]["unsafe_coverage"] - 0.10, 4),
        },
        "prod_f1_not_materially_worse": {
            "fresh_shipped": ship_prod["macro_f1"],
            "fresh_incumbent": inc_prod["macro_f1"],
            "pass": ship_prod["macro_f1"] >= round(inc_prod["macro_f1"] - 0.03, 4),
        },
        "span_continuity_top1": {
            "fresh_shipped": ship_span["top1_accuracy"],
            "fresh_incumbent": inc_span["top1_accuracy"],
            "pass": ship_span["top1_accuracy"] >= round(inc_span["top1_accuracy"] - 0.03, 4),
        },
        "demo_a_recorded": {"conf": demo["A"]["gateway_degradation_conf"], "floor": demo["A"]["floor"], "pass": demo["A"]["pass"]},
        "demo_d_recorded": {"conf": demo["D"]["gateway_degradation_conf"], "floor": demo["D"]["floor"], "pass": demo["D"]["pass"]},
        # the stricter pre-registered span macro-F1 reading, kept disclosed:
        "span_f1_stricter_reading_DISCLOSED_FAIL": {
            "fresh_shipped": ship_span["macro_f1"],
            "fresh_incumbent": inc_span["macro_f1"],
            "pass": ship_span["macro_f1"] >= round(inc_span["macro_f1"] - 0.03, 4),
        },
    }

    out = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "shipped_model_version": active["model_version"],
        "expected_model_version": recorded_ship["model_version"],
        "model_version_match": active["model_version"] == recorded_ship["model_version"],
        "dataset_integrity": integrity,
        "fresh_shipped": fresh_shipped,
        "fresh_incumbent_lr": fresh_incumbent,
        "recorded_vs_fresh_diffs": diffs,
        "all_blocks_match_record_4dp": all(not d for d in diffs.values()),
        "gate_reevaluation": gate,
        "gate_all_hard_clauses_pass": all(
            g["pass"] for k, g in gate.items() if not k.startswith("span_f1_stricter")
        ),
    }
    (OUT_DIR / "block_remeasure.json").write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")

    print(f"shipped artifact: {active['model_version']} (match={out['model_version_match']})")
    for block_key in blocks:
        m = fresh_shipped[block_key]
        b = m["business"]
        print(
            f"{block_key:<14} n={m['n_samples']:<4} F1={m['macro_f1']:.4f} top1={m['top1_accuracy']:.4f} "
            f"ECE={m['ece']:.4f} brier={m['brier']:.4f} safe={b['safe_auto_lane_coverage']} "
            f"unsafe={b['unsafe_coverage']} auto={b['auto_coverage']} | record-diff: {diffs[block_key] or 'MATCH'}"
        )
    print(f"gate hard clauses all pass (fresh numbers): {out['gate_all_hard_clauses_pass']}")
    print(f"stricter span-F1 reading (disclosed): pass={gate['span_f1_stricter_reading_DISCLOSED_FAIL']['pass']}")
    print(f"wrote {OUT_DIR / 'block_remeasure.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

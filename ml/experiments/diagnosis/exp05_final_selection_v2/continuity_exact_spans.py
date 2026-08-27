#!/usr/bin/env python
"""Exact-span continuity check (experiment exp05).

The production-frame candidate and the currently-deployed artifact are both
scored on the EXACT-SPAN dataset of docs/ml.md §8
(`backend/artifacts/sim_features.csv`, 2050 rows, 60 seeds — framed spans,
not detection windows), same temporal 60/20/20 split, test block only. This
is the continuity bridge: §8's table was measured on these frames, so a
regression here means the production-frame model forgets the easy frame —
not necessarily a deploy blocker (the frames differ by design), but it must
be measured and disclosed either way.

Run from the repo root:

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp05_final_selection_v2/continuity_exact_spans.py \
        --new-artifact <path-to-new-joblib>
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import joblib  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.services.diagnosis.features import FEATURE_NAMES, features_to_vector  # noqa: E402
from app.services.diagnosis.training import compute_metrics, temporal_split  # noqa: E402

EXACT_SPAN = BACKEND / "artifacts" / "sim_features.csv"
CURRENT_ARTIFACT = BACKEND / "artifacts" / "diagnosis_logistic_regression_v20260826T234303Z-c5434878.joblib"


def _score(df: pd.DataFrame, artifact: dict) -> dict:
    model, names = artifact["model"], artifact["feature_names"]
    X = np.asarray([features_to_vector(r, names) for r in df[FEATURE_NAMES].to_dict("records")])
    proba = model.predict_proba(X)
    classes = [str(c) for c in model.classes_]
    y_pred = np.asarray([classes[j] for j in proba.argmax(axis=1)])
    return compute_metrics(df["label"].to_numpy(), y_pred, proba, model.classes_, list(artifact["labels"]))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--new-artifact", type=Path, required=True)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()
    out_dir = args.out or Path(__file__).resolve().parent

    df = pd.read_csv(EXACT_SPAN)
    df["window_end"] = pd.to_datetime(df["window_end"], utc=True)
    _, _, test = temporal_split(df, 0.6, 0.2)

    current = joblib.load(CURRENT_ARTIFACT)
    new = joblib.load(args.new_artifact)
    result = {
        "dataset": "backend/artifacts/sim_features.csv (exact-span frames, docs/ml.md §8)",
        "test_rows": len(test),
        "current_artifact": {
            "model_version": current["model_version"],
            "test": _score(test, current),
        },
        "new_artifact": {
            "model_version": new["model_version"],
            "dataset": new.get("dataset"),
            "test": _score(test, new),
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "continuity_exact_spans.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    for key in ("current_artifact", "new_artifact"):
        m = result[key]["test"]
        b = m["business"]
        print(
            f"{key:<16} {result[key]['model_version']:<28} macroF1={m['macro_f1']:.4f} "
            f"top1={m['top1_accuracy']:.4f} top3={m['top3_accuracy']:.4f} ece={m['ece']:.4f} "
            f"safe={b['safe_auto_lane_coverage']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

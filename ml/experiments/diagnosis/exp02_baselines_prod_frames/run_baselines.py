#!/usr/bin/env python
"""Baselines on the production-frame dataset (experiment exp02).

Baselines BEFORE candidates (scientific order): the current heuristic and the
currently-deployed artifact (``diagnosis_logistic_regression_v20260826T234303Z
-c5434878`` — loaded explicitly by filename, NOT via the active pointer, so
the record stays valid after any redeploy) evaluated on the production-frame
dataset built in exp01. Full frame + the temporal val/test blocks the
candidates will be selected/reported on (same temporal_split call, same
fractions — identical blocks by construction).

Run from the repo root:

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp02_baselines_prod_frames/run_baselines.py
"""

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]  # ml/experiments/diagnosis/exp02.../script.py
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import joblib  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.services.diagnosis.features import FEATURE_NAMES, features_to_vector  # noqa: E402
from app.services.diagnosis.heuristic import HEURISTIC_VERSION, heuristic_diagnose  # noqa: E402
from app.services.diagnosis.taxonomy import CAUSES  # noqa: E402
from app.services.diagnosis.training import compute_metrics, temporal_split  # noqa: E402

DATASET = BACKEND / "artifacts" / "prod_frames_v1.csv"
CURRENT_ARTIFACT = BACKEND / "artifacts" / "diagnosis_logistic_regression_v20260826T234303Z-c5434878.joblib"
TRAIN_FRAC = 0.6
VAL_FRAC = 0.2


def _git_sha() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=10,
    )
    return out.stdout.strip() or "unknown"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate_artifact(block: pd.DataFrame, artifact: dict) -> tuple[dict, pd.DataFrame]:
    model = artifact["model"]
    names = artifact["feature_names"]
    X = np.asarray([features_to_vector(r, names) for r in block[FEATURE_NAMES].to_dict("records")])
    proba = model.predict_proba(X)
    classes = [str(c) for c in model.classes_]
    y_pred = np.asarray([classes[j] for j in proba.argmax(axis=1)])
    y_true = block["label"].to_numpy()
    metrics = compute_metrics(y_true, y_pred, proba, model.classes_, list(artifact["labels"]))
    rows = block[["window_end", "seed", "scenario", "label", "metric"]].copy()
    rows["pred"] = y_pred
    rows["confidence"] = proba.max(axis=1)
    rows["correct"] = y_pred == y_true
    return metrics, rows


def evaluate_heuristic(block: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    probas, preds = [], []
    for record in block[FEATURE_NAMES].to_dict("records"):
        feats = {name: float(record[name]) for name in FEATURE_NAMES}
        out = heuristic_diagnose(feats)
        preds.append(out["label"])
        probas.append([float(out["proba"].get(c, 0.0)) for c in CAUSES])
    proba = np.asarray(probas)
    y_pred = np.asarray(preds)
    y_true = block["label"].to_numpy()
    metrics = compute_metrics(y_true, y_pred, proba, np.asarray(CAUSES), list(CAUSES))
    rows = block[["window_end", "seed", "scenario", "label", "metric"]].copy()
    rows["pred"] = y_pred
    rows["confidence"] = proba.max(axis=1)
    rows["correct"] = y_pred == y_true
    return metrics, rows


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, default=DATASET)
    p.add_argument("--out", type=Path, default=None,
                   help="record dir (default: the script's own dir)")
    args = p.parse_args()
    exp_dir = args.out or Path(__file__).resolve().parent
    dataset = args.dataset
    df = pd.read_csv(dataset)
    df["window_end"] = pd.to_datetime(df["window_end"], utc=True)
    train, val, test = temporal_split(df, TRAIN_FRAC, VAL_FRAC)

    artifact = joblib.load(CURRENT_ARTIFACT)
    artifact_id = f"{artifact['algo']} {artifact['model_version']}"

    results: dict = {
        "dataset": {
            "path": str(dataset.resolve().relative_to(REPO_ROOT)),
            "rows": len(df),
            "sha256": _sha256(dataset),
            "split_fractions": {"train": TRAIN_FRAC, "val": VAL_FRAC},
            "split_counts": {"train": len(train), "val": len(val), "test": len(test)},
        },
        "baselines": {},
    }
    blocks = {"full": df, "val": val, "test": test}
    predictions: dict[str, dict[str, pd.DataFrame]] = {"current_artifact": {}, "heuristic": {}}
    for name, block in blocks.items():
        m_art, rows_art = evaluate_artifact(block, artifact)
        m_heur, rows_heur = evaluate_heuristic(block)
        results["baselines"].setdefault("current_artifact", {})[name] = m_art
        results["baselines"].setdefault("heuristic", {})[name] = m_heur
        predictions["current_artifact"][name] = rows_art
        predictions["heuristic"][name] = rows_heur

    config = {
        "experiment": "exp02_baselines_prod_frames",
        "script": str(Path(__file__).relative_to(REPO_ROOT)),
        "argv": sys.argv,
        "git_sha": _git_sha(),
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "baselines": {
            "current_artifact": {
                "file": CURRENT_ARTIFACT.name,
                "model": artifact_id,
                "trained_on": artifact.get("dataset"),
                "note": "loaded by explicit filename, not the active pointer",
            },
            "heuristic": {"model": f"diagnosis-heuristic {HEURISTIC_VERSION}"},
        },
    }
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (exp_dir / "metrics.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    for baseline, per_block in predictions.items():
        per_block["test"].to_csv(exp_dir / f"predictions_{baseline}_test.csv", index=False)

    for baseline in ("current_artifact", "heuristic"):
        print(f"\n=== {baseline} ===")
        for block_name in ("full", "val", "test"):
            m = results["baselines"][baseline][block_name]
            b = m["business"]
            print(
                f"{block_name:<5} n={m['n_samples']:<4} macroF1={m['macro_f1']:.4f} "
                f"top1={m['top1_accuracy']:.4f} top3={m['top3_accuracy']:.4f} "
                f"ece={m['ece']:.4f} brier={m['brier']:.4f} "
                f"safe={b['safe_auto_lane_coverage']} "
                f"(auto={b['auto_coverage']} unsafe={b['unsafe_coverage']} "
                f"falsefire={b['false_fire_rate']})"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

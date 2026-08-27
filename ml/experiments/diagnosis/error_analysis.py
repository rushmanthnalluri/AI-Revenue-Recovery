#!/usr/bin/env python
"""Row-level failure analysis for a trained diagnosis artifact on a dataset.

Reproduces the temporal split (same call/fractions as training), scores the
val/test blocks, and dumps per-row predictions plus the failure-mode cuts the
experiment write-ups use: per-class confusion, diluted-window failure modes
(by scenario / detection metric / multi-incident overlap / window volume),
and calibration failure modes (overconfident-wrong at confidence >= 0.85).

Run from the repo root, e.g.:

    backend/.venv/Scripts/python ml/experiments/diagnosis/error_analysis.py \
        --dataset backend/artifacts/prod_frames_v1.csv \
        --artifact ml/experiments/diagnosis/exp03_calibration_candidates/artifacts/diagnosis_random_forest_v20260827T123306Z-609981e3.joblib \
        --out ml/experiments/diagnosis/exp03_calibration_candidates
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # ml/experiments/diagnosis/error_analysis.py
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import joblib  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.services.diagnosis.features import FEATURE_NAMES, features_to_vector  # noqa: E402
from app.services.diagnosis.taxonomy import AUTO_RECOVERABLE_CAUSES  # noqa: E402
from app.services.diagnosis.training import AUTO_EXECUTE_THRESHOLD, temporal_split  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--artifact", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--train-frac", type=float, default=0.6)
    p.add_argument("--val-frac", type=float, default=0.2)
    args = p.parse_args()

    df = pd.read_csv(args.dataset)
    df["window_end"] = pd.to_datetime(df["window_end"], utc=True)
    _, val, test = temporal_split(df, args.train_frac, args.val_frac)

    artifact = joblib.load(args.artifact)
    model, names = artifact["model"], artifact["feature_names"]

    out_rows = []
    for block_name, block in (("val", val), ("test", test)):
        X = np.asarray([features_to_vector(r, names) for r in block[FEATURE_NAMES].to_dict("records")])
        proba = model.predict_proba(X)
        classes = [str(c) for c in model.classes_]
        for i, (_, row) in enumerate(block.iterrows()):
            pred = classes[int(proba[i].argmax())]
            conf = float(proba[i].max())
            true = row["label"]
            out_rows.append(
                {
                    "block": block_name,
                    "window_end": row["window_end"].isoformat(),
                    "seed": row["seed"],
                    "scenario": row["scenario"],
                    "detection_metric": row["metric"],
                    "true": true,
                    "pred": pred,
                    "confidence": round(conf, 6),
                    "correct": pred == true,
                    "overconfident_wrong": bool(conf >= AUTO_EXECUTE_THRESHOLD and pred != true),
                    "true_auto_recoverable": true in AUTO_RECOVERABLE_CAUSES,
                    "pred_auto_recoverable": pred in AUTO_RECOVERABLE_CAUSES,
                    "volume": row["volume"],
                    "failure_rate_delta": row["failure_rate_delta"],
                    "multi_overlap": bool(str(row.get("overlapping_entity_ids", "")).find("|") >= 0)
                    if "overlapping_entity_ids" in block.columns
                    else None,
                    "proba": {c: round(float(proba[i, j]), 6) for j, c in enumerate(classes)},
                }
            )
    pred_df = pd.DataFrame(out_rows)
    args.out.mkdir(parents=True, exist_ok=True)
    pred_df.drop(columns=["proba"]).to_csv(args.out / "row_predictions.csv", index=False)

    def cuts(block: pd.DataFrame) -> dict:
        errors = block[~block["correct"]]
        return {
            "n": len(block),
            "errors": int(len(errors)),
            "error_rate": round(len(errors) / max(len(block), 1), 4),
            "by_true_class": {
                c: {
                    "support": int((block["true"] == c).sum()),
                    "errors": int((errors["true"] == c).sum()),
                    "top_confusions": errors[errors["true"] == c]["pred"].value_counts().head(3).to_dict(),
                }
                for c in sorted(block["true"].unique())
            },
            "by_scenario_error_rate": block.groupby("scenario")["correct"]
            .agg(["count", "mean"])
            .round(4)
            .to_dict(),
            "by_detection_metric_error_rate": block.groupby("detection_metric")["correct"]
            .agg(["count", "mean"])
            .round(4)
            .to_dict(),
            "multi_overlap": {
                "rows": int(block["multi_overlap"].sum()) if block["multi_overlap"].notna().any() else None,
                "error_rate": round(
                    float((~block.loc[block["multi_overlap"] == True, "correct"]).mean()), 4
                )
                if block["multi_overlap"].notna().any() and block["multi_overlap"].any()
                else None,
            },
            "overconfident_wrong": {
                "rows": int(block["overconfident_wrong"].sum()),
                "examples": block[block["overconfident_wrong"]]
                .sort_values("confidence", ascending=False)
                .head(10)[["block", "scenario", "true", "pred", "confidence", "detection_metric"]]
                .to_dict("records"),
            },
            "confidence_quantiles_correct": block.loc[block["correct"], "confidence"]
            .quantile([0.1, 0.5, 0.9])
            .round(4)
            .to_dict(),
            "confidence_quantiles_wrong": block.loc[~block["correct"], "confidence"]
            .quantile([0.1, 0.5, 0.9])
            .round(4)
            .to_dict()
            if (~block["correct"]).any()
            else {},
        }

    analysis = {
        "dataset": str(args.dataset),
        "artifact": str(args.artifact),
        "model_version": artifact.get("model_version"),
        "val": cuts(pred_df[pred_df["block"] == "val"]),
        "test": cuts(pred_df[pred_df["block"] == "test"]),
    }
    (args.out / "error_analysis.json").write_text(
        json.dumps(analysis, indent=2, default=str) + "\n", encoding="utf-8"
    )
    t = analysis["test"]
    print(
        f"test block: n={t['n']} errors={t['errors']} "
        f"overconfident_wrong={t['overconfident_wrong']['rows']}"
    )
    print("per-class test errors:")
    for c, s in t["by_true_class"].items():
        print(f"  {c:<38} support={s['support']:<3} errors={s['errors']:<3} {s['top_confusions']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

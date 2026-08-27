#!/usr/bin/env python
"""exp06 — build the ship artifact: one candidate on dual_frame_v3.

Why LR raw and not the exp06 validation winner (gradient_boosting+isotonic):
the deploy rule applies HARD operational constraints before the validation
preference — the v3 val winner fails the demo-suite operational check
(auto lane unreachable on the demo's tight window, 0.806 < 0.867; measured
in candidate_frames.json). Among the four candidates passing BOTH hard
constraints (prod-frame deploy gate + demo operational check), LR raw ranks
first by the pre-registered validation rule (val safe 0.1254 vs RF 0.0689,
RF+sig 0.0419, RF+iso 0.0278). See DECISION_log.md / SHIP_VERDICT.md.

The estimator is identical to the one candidate_frames.py evaluated (same
data, same presplit, same seed, same build_estimator call — deterministic).

Run from the repo root:

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp06_dual_frame_v3/ship_candidate.py --algo random_forest --calibration isotonic
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import joblib  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import sklearn  # noqa: E402

from app.services.diagnosis.features import FEATURE_NAMES, features_to_vector  # noqa: E402
from app.services.diagnosis.taxonomy import CAUSES  # noqa: E402
from app.services.diagnosis.training import (  # noqa: E402
    build_estimator,
    compute_metrics,
    make_model_version,
    temporal_split,
)

SEED = 42


def _score(est, block, labels):
    X = np.asarray([features_to_vector(r) for r in block[FEATURE_NAMES].to_dict("records")])
    proba = est.predict_proba(X)
    classes = [str(c) for c in est.classes_]
    y_pred = np.asarray([classes[j] for j in proba.argmax(axis=1)])
    return compute_metrics(block["label"].to_numpy(), y_pred, proba, est.classes_, labels)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--algo", default="random_forest")
    ap.add_argument("--calibration", default="isotonic")
    args = ap.parse_args()
    exp_dir = Path(__file__).resolve().parent
    prod = pd.read_csv(BACKEND / "artifacts" / "prod_frames_v2.csv")
    span = pd.read_csv(BACKEND / "artifacts" / "sim_features.csv")
    for frame in (prod, span):
        frame["window_end"] = pd.to_datetime(frame["window_end"], utc=True)
    prod_train, prod_val, prod_test = temporal_split(prod, 0.6, 0.2)
    span_train, span_val, span_test = temporal_split(span, 0.6, 0.2)
    train = pd.concat([prod_train, span_train], ignore_index=True)
    val = pd.concat([prod_val, span_val], ignore_index=True)

    X_train = np.asarray([features_to_vector(r) for r in train[FEATURE_NAMES].to_dict("records")])
    est = build_estimator(args.algo, SEED, args.calibration)
    est.fit(X_train, train["label"].to_numpy())

    df_all = pd.concat([train, val, prod_test, span_test], ignore_index=True)
    version = make_model_version(df_all, SEED)
    trained_at = datetime.now(timezone.utc).isoformat()
    labels = [c for c in CAUSES if c in set(df_all["label"])]
    metrics = {
        "val": _score(est, val, labels),
        "test_prod_detection_window": _score(est, prod_test, labels),
        "test_exact_span_frame": _score(est, span_test, labels),
    }
    from app.services.diagnosis.training import candidate_name

    candidate = candidate_name(args.algo, args.calibration)
    name = f"diagnosis_{candidate}_{version}.joblib"
    payload = {
        "model": est,
        "algo": args.algo,
        "calibration": args.calibration,
        "candidate": candidate,
        "model_version": version,
        "trained_at": trained_at,
        "feature_names": list(FEATURE_NAMES),
        "labels": labels,
        "dataset": "dual_frame_v3: prod_frames_v2 (506) + sim_features (2050), per-source temporal split",
        "sklearn_version": sklearn.__version__,
        "metrics": metrics,
    }
    out_dir = exp_dir / f"artifacts_{args.algo}_{args.calibration}"
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(payload, out_dir / name)
    pointer = {
        "artifact": name,
        "algo": args.algo,
        "calibration": args.calibration,
        "candidate": candidate,
        "model_version": version,
        "trained_at": trained_at,
        "dataset": payload["dataset"],
        "val_macro_f1": metrics["val"]["macro_f1"],
        "test_macro_f1": metrics["test_prod_detection_window"]["macro_f1"],
        "test_top1_accuracy": metrics["test_prod_detection_window"]["top1_accuracy"],
        "test_top3_accuracy": metrics["test_prod_detection_window"]["top3_accuracy"],
        "test_ece": metrics["test_prod_detection_window"]["ece"],
        "test_brier": metrics["test_prod_detection_window"]["brier"],
        "test_safe_auto_lane_coverage": metrics["test_prod_detection_window"]["business"][
            "safe_auto_lane_coverage"
        ],
    }
    (out_dir / "diagnosis_active.json").write_text(
        json.dumps(pointer, indent=2) + "\n", encoding="utf-8"
    )
    (exp_dir / "ship_metrics.json").write_text(
        json.dumps({"model_version": version, "metrics": metrics}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"model_version: {version}")
    for split, m in metrics.items():
        b = m["business"]
        print(
            f"{split:<28} n={m['n_samples']:>4} F1={m['macro_f1']:.4f} top1={m['top1_accuracy']:.4f} "
            f"top3={m['top3_accuracy']:.4f} ece={m['ece']:.4f} brier={m['brier']:.4f} "
            f"safe={b['safe_auto_lane_coverage']} (auto={b['auto_coverage']} "
            f"unsafe={b['unsafe_coverage']} ff={b['false_fire_rate']})"
        )
    print(f"artifact: {out_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

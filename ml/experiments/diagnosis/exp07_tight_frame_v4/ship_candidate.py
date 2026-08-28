#!/usr/bin/env python
"""exp07 — build the ship artifact: one candidate on v4.

Refits the gate-passing candidate chosen by run_v4.py on the SAME data,
SAME per-source presplit, SAME seed (deterministic: seeded RF/GB, deterministic
LR, TimeSeriesSplit calibration), then RE-SCORES every frame and asserts the
key numbers match metrics.json at 4dp — the shipped bytes are the scored
estimator, not a lookalike. Writes the artifact + an active-pointer JSON into
``artifacts_<algo>_<calibration>/`` (the swap into backend/artifacts/ is a
separate, deliberate step).

Run from the repo root:

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp07_tight_frame_v4/ship_candidate.py --algo random_forest --calibration isotonic
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
EXP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(EXP_DIR))

import joblib  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import sklearn  # noqa: E402

from app.services.diagnosis.features import FEATURE_NAMES, features_to_vector  # noqa: E402
from app.services.diagnosis.taxonomy import CAUSES  # noqa: E402
from app.services.diagnosis.training import (  # noqa: E402
    build_estimator,
    candidate_name,
    compute_metrics,
    make_model_version,
    temporal_split,
)
from run_v4 import _demo_frame, _demo_score  # noqa: E402
from scripts.demo_run import config_a, config_b, config_d  # noqa: E402

SEED = 42


def _score(est, block: pd.DataFrame, labels: list[str]) -> dict:
    X = np.asarray([features_to_vector(r) for r in block[FEATURE_NAMES].to_dict("records")])
    proba = est.predict_proba(X)
    classes = [str(c) for c in est.classes_]
    y_pred = np.asarray([classes[j] for j in proba.argmax(axis=1)])
    return compute_metrics(block["label"].to_numpy(), y_pred, proba, est.classes_, labels)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--algo", required=True)
    ap.add_argument("--calibration", default="none")
    ap.add_argument("--aug-csv", type=Path, default=None,
                    help="iteration-2 augmentation CSV (must match the run_v4 invocation)")
    ap.add_argument("--metrics", default="metrics.json",
                    help="run_v4 metrics file to verify against (e.g. metrics_v4b.json)")
    args = ap.parse_args()
    candidate = candidate_name(args.algo, args.calibration)

    recorded = json.loads((EXP_DIR / args.metrics).read_text(encoding="utf-8"))
    assert candidate in recorded["candidates"], f"{candidate} not in {args.metrics}"
    rec = recorded["candidates"][candidate]

    prod = pd.read_csv(BACKEND / "artifacts" / "prod_frames_v3.csv")
    tight = pd.read_csv(BACKEND / "artifacts" / "tight_frames_v1.csv")
    span = pd.read_csv(BACKEND / "artifacts" / "sim_features.csv")
    dataset_desc = (
        "v4: prod_frames_v3 (stabilized engine, 70k/day cycle) + tight_frames_v1 "
        "(ad-hoc 180/240-min frames) + sim_features (exact spans), per-source temporal split"
    )
    if args.aug_csv is not None:
        aug = pd.read_csv(args.aug_csv)
        aug["window_end"] = pd.to_datetime(aug["window_end"], utc=True)
        dataset_desc = (
            "v4b: v4 + aug_pure_sr_v1 TRAIN-ONLY (latency_multiplier=1.0 "
            "gateway-degradation frames at 70k/30k events-day, the measured "
            "demo-A OOD hole), per-source temporal split"
        )
    blocks = {}
    for name, df in (("prod", prod), ("tight", tight), ("span", span)):
        df["window_end"] = pd.to_datetime(df["window_end"], utc=True)
        blocks[name] = temporal_split(df, 0.6, 0.2)
    train = pd.concat([b[0] for b in blocks.values()], ignore_index=True)
    val = pd.concat([b[1] for b in blocks.values()], ignore_index=True)
    if args.aug_csv is not None:
        train = pd.concat([train, aug], ignore_index=True)

    X_train = np.asarray([features_to_vector(r) for r in train[FEATURE_NAMES].to_dict("records")])
    est = build_estimator(args.algo, SEED, args.calibration)
    est.fit(X_train, train["label"].to_numpy())

    print("recomputing demo frames for verification ...", flush=True)
    demo_feats = {"A": _demo_frame(config_a(), 240), "B": _demo_frame(config_b(), 180), "D": _demo_frame(config_d(), 180)}

    labels = [c for c in CAUSES if c in set(pd.concat([train, val])["label"])]
    metrics = {
        "val": _score(est, val, labels),
        "prod_v3_test": _score(est, blocks["prod"][2], labels),
        "tight_test": _score(est, blocks["tight"][2], labels),
        "span_test": _score(est, blocks["span"][2], labels),
        "demo": _demo_score(est, demo_feats),
    }

    # The shipped bytes must be the scored estimator: key numbers at 4dp.
    checks = [
        ("prod_v3_test.macro_f1", metrics["prod_v3_test"]["macro_f1"], rec["prod_v3_test"]["macro_f1"]),
        ("prod_v3_test.safe", metrics["prod_v3_test"]["business"]["safe_auto_lane_coverage"], rec["prod_v3_test"]["business"]["safe_auto_lane_coverage"]),
        ("span_test.top1", metrics["span_test"]["top1_accuracy"], rec["span_test"]["top1_accuracy"]),
        ("demo.A", metrics["demo"]["A"]["gateway_degradation_conf"], rec["demo"]["A"]["gateway_degradation_conf"]),
        ("demo.D", metrics["demo"]["D"]["gateway_degradation_conf"], rec["demo"]["D"]["gateway_degradation_conf"]),
    ]
    for name, got, want in checks:
        assert abs(got - want) < 5e-5, f"refit mismatch on {name}: {got} vs recorded {want}"
    print("refit matches metrics.json on all key numbers (4dp)")

    df_all = pd.concat([train, val, blocks["prod"][2], blocks["tight"][2], blocks["span"][2]], ignore_index=True)
    version = make_model_version(df_all, SEED)
    trained_at = datetime.now(timezone.utc).isoformat()
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
        "dataset": dataset_desc,
        "sklearn_version": sklearn.__version__,
        "metrics": metrics,
    }
    out_dir = EXP_DIR / f"artifacts_{args.algo}_{args.calibration}"
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
        "test_macro_f1": metrics["prod_v3_test"]["macro_f1"],
        "test_top1_accuracy": metrics["prod_v3_test"]["top1_accuracy"],
        "test_top3_accuracy": metrics["prod_v3_test"]["top3_accuracy"],
        "test_ece": metrics["prod_v3_test"]["ece"],
        "test_brier": metrics["prod_v3_test"]["brier"],
        "test_safe_auto_lane_coverage": metrics["prod_v3_test"]["business"]["safe_auto_lane_coverage"],
    }
    (out_dir / "diagnosis_active.json").write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")
    (EXP_DIR / "ship_metrics.json").write_text(
        json.dumps({"candidate": candidate, "model_version": version, "metrics": metrics}, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"model_version: {version}")
    for split, m in metrics.items():
        if split == "demo":
            continue
        b = m["business"]
        print(
            f"{split:<14} n={m['n_samples']:>4} F1={m['macro_f1']:.4f} top1={m['top1_accuracy']:.4f} "
            f"ece={m['ece']:.4f} safe={b['safe_auto_lane_coverage']} "
            f"(auto={b['auto_coverage']} unsafe={b['unsafe_coverage']} ff={b['false_fire_rate']})"
        )
    print("demo:", json.dumps({k: v["gateway_degradation_conf"] for k, v in metrics["demo"].items()}))
    print(f"artifact: {out_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""exp06 — dual-frame v3: production detection windows + exact-span frames.

Why this exists (full story in DECISION_log.md): the v2 model (exp05) won on
production frames but REGRESSED the demo's tight 180-min ad-hoc detection
window (gateway_degradation confidence 0.239 vs the old artifact's 1.000 —
measured) because v2 contains ONLY 12h scheduled windows. Production serves
both frame families: scheduled 12h passes AND ad-hoc/tight windows (demo,
API investigations). v3 = prod_frames_v2 (506 rows) + sim_features.csv
(2050 rows, docs/ml.md §8) = 2556 rows covering the full frame continuum.

Split: PER-SOURCE temporal 60/20/20 (so the exp05 v2 test block and the §8
exact-span test block stay EXACTLY the held-out rows — the deploy gate and
the continuity check remain honest), then blocks concatenated for training.

Run from the repo root:

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp06_dual_frame_v3/run_v3.py
"""

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.services.diagnosis.features import FEATURE_NAMES, features_to_vector  # noqa: E402
from app.services.diagnosis.training import (  # noqa: E402
    SELECTION_RULE,
    compute_metrics,
    save_artifacts,
    temporal_split,
    train_and_compare,
)

PROD = BACKEND / "artifacts" / "prod_frames_v2.csv"
SPAN = BACKEND / "artifacts" / "sim_features.csv"
SEED = 42


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _score(estimator, block: pd.DataFrame, labels: list[str]) -> dict:
    X = np.asarray([features_to_vector(r) for r in block[FEATURE_NAMES].to_dict("records")])
    proba = estimator.predict_proba(X)
    classes = [str(c) for c in estimator.classes_]
    y_pred = np.asarray([classes[j] for j in proba.argmax(axis=1)])
    return compute_metrics(block["label"].to_numpy(), y_pred, proba, estimator.classes_, labels)


def main() -> int:
    exp_dir = Path(__file__).resolve().parent
    prod = pd.read_csv(PROD)
    span = pd.read_csv(SPAN)
    for df, source in ((prod, "prod_detection_window"), (span, "exact_span_frame")):
        df["frame_source"] = source
        df["window_end"] = pd.to_datetime(df["window_end"], utc=True)

    prod_train, prod_val, prod_test = temporal_split(prod, 0.6, 0.2)
    span_train, span_val, span_test = temporal_split(span, 0.6, 0.2)
    train = pd.concat([prod_train, span_train], ignore_index=True)
    val = pd.concat([prod_val, span_val], ignore_index=True)
    test = pd.concat([prod_test, span_test], ignore_index=True)
    df = pd.concat([train, val, test], ignore_index=True)

    result = train_and_compare(df, seed=SEED, presplit=(train, val, test))
    artifact_path = save_artifacts(result, exp_dir / "artifacts", "v3: prod_frames_v2 + sim_features (dual-frame)")

    best = result.best
    per_source = {
        "prod_detection_window_test": _score(best.estimator, prod_test, result.labels),
        "exact_span_frame_test": _score(best.estimator, span_test, result.labels),
    }

    config = {
        "experiment": "exp06_dual_frame_v3",
        "script": str(Path(__file__).relative_to(REPO_ROOT)),
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_sha": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=10,
        ).stdout.strip(),
        "dataset": {
            "version": "dual_frame_v3",
            "rows": len(df),
            "sources": {
                "prod_detection_window": {
                    "path": "backend/artifacts/prod_frames_v2.csv",
                    "rows": len(prod), "sha256": _sha256(PROD),
                    "split": {"train": len(prod_train), "val": len(prod_val), "test": len(prod_test)},
                },
                "exact_span_frame": {
                    "path": "backend/artifacts/sim_features.csv",
                    "rows": len(span), "sha256": _sha256(SPAN),
                    "split": {"train": len(span_train), "val": len(span_val), "test": len(span_test)},
                },
            },
            "split_policy": "per-source temporal 0.6/0.2/0.2 (exp05/§8 test blocks preserved exactly)",
        },
        "seed": SEED,
        "selection_rule": SELECTION_RULE,
        "model_version": result.model_version,
    }
    metrics = {
        "selected_candidate": result.best_algo,
        "candidates": {
            name: {"val": r.val_metrics, "test": r.test_metrics}
            for name, r in result.algo_results.items()
        },
        "selected_per_source_test": per_source,
    }
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (exp_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    cm = result.best.test_metrics["confusion_matrix"]
    lines = ["true\\predicted," + ",".join(cm["labels"])]
    for label, row in zip(cm["labels"], cm["matrix"]):
        lines.append(label + "," + ",".join(str(v) for v in row))
    (exp_dir / "confusion_matrix.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"selected: {result.best_algo} @ {result.model_version}")
    for name, r in result.algo_results.items():
        vb, tb = r.val_metrics["business"], r.test_metrics["business"]
        print(
            f"  {name:<32} val safe={vb['safe_auto_lane_coverage']:>7} F1={r.val_metrics['macro_f1']:.4f} "
            f"ece={r.val_metrics['ece']:.4f} | test F1={r.test_metrics['macro_f1']:.4f} "
            f"safe={tb['safe_auto_lane_coverage']:>7}"
        )
    for source, m in per_source.items():
        b = m["business"]
        print(
            f"{source}: n={m['n_samples']} F1={m['macro_f1']:.4f} top1={m['top1_accuracy']:.4f} "
            f"ece={m['ece']:.4f} safe={b['safe_auto_lane_coverage']} "
            f"(auto={b['auto_coverage']} unsafe={b['unsafe_coverage']} ff={b['false_fire_rate']})"
        )
    print(f"artifact: {artifact_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

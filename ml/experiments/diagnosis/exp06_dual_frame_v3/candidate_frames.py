#!/usr/bin/env python
"""exp06 companion: every v3 candidate x (prod-frame test, exact-span test,
demo-D operational frame). Retrains the 9 candidates identically to run_v3.py
(same data, seed, presplit) and scores each on all three frames, so the deploy
decision can apply the hard operational constraint (demo suite green) before
the validation preference rule. Writes candidate_frames.json.

Run from the repo root:

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp06_dual_frame_v3/candidate_frames.py
"""

import json
import sys
import tempfile
import shutil
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import sqlalchemy as sa  # noqa: E402

from app.models import Incident  # noqa: E402
from app.schemas.detection import DetectionRunRequest  # noqa: E402
from app.services.detection.engine import run_detection  # noqa: E402
from app.services.diagnosis.features import (  # noqa: E402
    FEATURE_NAMES,
    compute_features_for_incident,
    features_to_vector,
)
from app.services.diagnosis.taxonomy import CAUSES  # noqa: E402
from app.services.diagnosis.training import (  # noqa: E402
    ALGO_ORDER,
    CALIBRATION_MODES,
    build_estimator,
    candidate_name,
    compute_metrics,
    temporal_split,
)
from app.simulator.cli import make_session  # noqa: E402
from app.simulator.engine import run_simulation  # noqa: E402
from scripts.demo_run import _incident_window_utc, config_d  # noqa: E402

SEED = 42


def _score(est, block, labels):
    X = np.asarray([features_to_vector(r) for r in block[FEATURE_NAMES].to_dict("records")])
    proba = est.predict_proba(X)
    classes = [str(c) for c in est.classes_]
    y_pred = np.asarray([classes[j] for j in proba.argmax(axis=1)])
    return compute_metrics(block["label"].to_numpy(), y_pred, proba, est.classes_, labels)


def main() -> int:
    exp_dir = Path(__file__).resolve().parent
    prod = pd.read_csv(BACKEND / "artifacts" / "prod_frames_v2.csv")
    span = pd.read_csv(BACKEND / "artifacts" / "sim_features.csv")
    for frame in (prod, span):
        frame["window_end"] = pd.to_datetime(frame["window_end"], utc=True)
    prod_train, _, prod_test = temporal_split(prod, 0.6, 0.2)
    span_train, _, span_test = temporal_split(span, 0.6, 0.2)
    train = pd.concat([prod_train, span_train], ignore_index=True)
    X_train = np.asarray([features_to_vector(r) for r in train[FEATURE_NAMES].to_dict("records")])
    y_train = train["label"].to_numpy()

    # demo D operational frame (features computed once, shared by candidates)
    workdir = Path(tempfile.mkdtemp(prefix="demoD_frames_"))
    session = make_session(f"sqlite:///{workdir / 't.db'}")
    cfg = config_d()
    run_simulation(cfg, session)
    inc_start, inc_end = _incident_window_utc(cfg)
    run_detection(session, DetectionRunRequest(
        metrics=["payment_success_rate"], window_minutes=180, bucket_minutes=10,
        baseline_buckets=6, as_of=inc_end + timedelta(minutes=25)))
    demo_feats = None
    for inc in session.scalars(sa.select(Incident)):
        demo_feats = compute_features_for_incident(session, inc)
    shutil.rmtree(workdir, ignore_errors=True)
    assert demo_feats is not None, "demo D detection produced no incident"

    labels = list(CAUSES)
    out = {}
    for algo in ALGO_ORDER:
        for cal in CALIBRATION_MODES:
            name = candidate_name(algo, cal)
            est = build_estimator(algo, SEED, cal)
            est.fit(X_train, y_train)
            demo_proba = est.predict_proba(
                np.asarray([features_to_vector(demo_feats)])
            )[0]
            demo_top = sorted(
                zip([str(c) for c in est.classes_], demo_proba), key=lambda t: -t[1]
            )[:3]
            out[name] = {
                "prod_test": _score(est, prod_test, labels),
                "span_test": _score(est, span_test, labels),
                "demo_d": {
                    "top3": [[c, round(float(p), 4)] for c, p in demo_top],
                    "gateway_degradation_conf": round(
                        float(demo_proba[list(est.classes_).index("gateway_degradation")]), 4
                    ),
                    "strategy_conf_upper_bound": round(float(demo_proba.max()) * 0.98, 4),
                    "auto_lane_reachable": bool(demo_proba.max() * 0.98 >= 0.85),
                },
            }
            print(
                f"{name:<32} prod safe={out[name]['prod_test']['business']['safe_auto_lane_coverage']:>7} "
                f"unsafe={out[name]['prod_test']['business']['unsafe_coverage']:>7} "
                f"F1={out[name]['prod_test']['macro_f1']:.4f} | span top1={out[name]['span_test']['top1_accuracy']:.4f} "
                f"| demoD gd={out[name]['demo_d']['gateway_degradation_conf']:.4f} "
                f"auto={out[name]['demo_d']['auto_lane_reachable']}"
            )
    (exp_dir / "candidate_frames.json").write_text(
        json.dumps(out, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

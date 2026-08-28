#!/usr/bin/env python
"""exp07 — v4 training run: prod_frames_v3 + tight_frames_v1 + exact spans.

Dataset (the three exp06 follow-ups, built by build_dataset.py):

- ``prod_frames_v3``   — scheduled 12h detection windows, REBUILT on the
  stabilized detection engine (fresh seeds 6000-6143, density cycle extended
  to 70k events/day; supersedes the contaminated v2, exp04/CONTAMINATION.md).
- ``tight_frames_v1``  — NEW: ad-hoc tight frames (demo-shape 180/240-min
  success-rate passes + all-metric 180-min passes, anchored gt_end+25min) —
  the frame family whose absence sank v2 on demo D (0.239, exp06).
- ``sim_features.csv`` — the §8 exact-span dataset (2050 rows, unchanged) for
  exact-span continuity.

Split: PER-SOURCE temporal 60/20/20 (each family's held-out block stays
exact; the span test block is the same rows §8/exp06 reported against).

Gate (pre-registered for exp07, mirroring the assignment + exp05/exp06
clauses; applied to every candidate AND re-measured for the incumbent on
the same blocks — no carried-over numbers):

1. prod-frame gate on the prod_frames_v3 test block vs the incumbent:
   safe_auto_lane_coverage strictly better, unsafe_coverage materially lower
   (<= incumbent - 0.10), macro-F1 not materially worse (>= incumbent - 0.03).
2. exact-span continuity on the sim_features test block:
   top-1 >= incumbent - 0.03 and macro-F1 >= incumbent - 0.03.
3. demo operating points on the REAL demo frames (same configs, same request
   shape, measured offline exactly as exp06 did for D):
   scenario A: gateway_degradation top-1 with confidence >= 0.867;
   scenario D: gateway_degradation top-1 with confidence >= 0.944.
   (scenario B measured too — same shape as D, TIMEOUT pick -> >= 0.867.)
4. tests/demo 10/10 and the full backend suite green with the shipped
   pointer (verified after the swap; failure rolls back).

Ship rule (exp06 convention): hard clauses 1-3 first; among passers, the
pre-registered validation rule (SELECTION_RULE) ranks. If none pass:
NO-SHIP, incumbent stays, the constraint map is the deliverable.

Run from the repo root:

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp07_tight_frame_v4/run_v4.py
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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
    SELECTION_RULE,
    build_estimator,
    candidate_name,
    compute_metrics,
    load_active_artifact,
    select_candidate,
    temporal_split,
    train_and_compare,
    AlgoResult,
)
from app.simulator.cli import make_session  # noqa: E402
from app.simulator.engine import run_simulation  # noqa: E402
from scripts.demo_run import _incident_window_utc, config_a, config_b, config_d  # noqa: E402

SEED = 42
PROD_V3 = BACKEND / "artifacts" / "prod_frames_v3.csv"
TIGHT = BACKEND / "artifacts" / "tight_frames_v1.csv"
SPAN = BACKEND / "artifacts" / "sim_features.csv"
PROD_V2 = BACKEND / "artifacts" / "prod_frames_v2.csv"  # held-out legacy reference only

# Pre-registered demo operating points (exp06/SHIP_VERDICT.md):
# A: auto-lane pick is TIMEOUT (action-fit 0.98) -> 0.85/0.98 = 0.8673
# D: auto-lane pick is SOFT_DECLINE (action-fit 0.90) -> 0.85/0.90 = 0.9444
DEMO_FLOORS = {"A": 0.867, "B": 0.867, "D": 0.944}
# Gate materiality bands (pre-registered above in the module docstring):
UNSAFE_MATERIAL_DROP = 0.10
F1_MATERIAL_BAND = 0.03
SPAN_CONTINUITY_BAND = 0.03


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _score(est, block: pd.DataFrame, labels: list[str]) -> dict:
    X = np.asarray([features_to_vector(r) for r in block[FEATURE_NAMES].to_dict("records")])
    proba = est.predict_proba(X)
    classes = [str(c) for c in est.classes_]
    y_pred = np.asarray([classes[j] for j in proba.argmax(axis=1)])
    return compute_metrics(block["label"].to_numpy(), y_pred, proba, est.classes_, labels)


def _demo_frame(config, window_minutes: int) -> dict:
    """The exact frame the demo diagnoses: same sim config, same detection
    request shape, same incident pick (max revenue_at_risk among success-rate
    incidents), same feature code path as DiagnosisService."""
    workdir = Path(tempfile.mkdtemp(prefix="exp07_demo_"))
    try:
        session = make_session(f"sqlite:///{workdir / 't.db'}")
        run_simulation(config, session)
        _, inc_end = _incident_window_utc(config)
        run_detection(
            session,
            DetectionRunRequest(
                metrics=["payment_success_rate"],
                window_minutes=window_minutes,
                bucket_minutes=10,
                baseline_buckets=6,
                as_of=inc_end + timedelta(minutes=25),
            ),
        )
        sr = [
            i
            for i in session.scalars(sa.select(Incident)).all()
            if i.metric == "payment_success_rate"
        ]
        assert sr, "demo detection persisted no success-rate incident"
        chosen = max(sr, key=lambda i: i.revenue_at_risk_paise or 0)
        return compute_features_for_incident(session, chosen)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _demo_score(est, demo_feats: dict[str, dict]) -> dict:
    out = {}
    for name, feats in demo_feats.items():
        proba = est.predict_proba(np.asarray([features_to_vector(feats)]))[0]
        classes = [str(c) for c in est.classes_]
        top = sorted(zip(classes, proba), key=lambda t: -t[1])[:3]
        gd = float(proba[classes.index("gateway_degradation")])
        floor = DEMO_FLOORS[name]
        out[name] = {
            "top3": [[c, round(float(p), 4)] for c, p in top],
            "gateway_degradation_conf": round(gd, 4),
            "floor": floor,
            "pass": bool(top[0][0] == "gateway_degradation" and gd >= floor),
        }
    return out


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--aug-csv",
        type=Path,
        default=None,
        help="iteration-2 augmentation CSV. Rows join the TRAINING BLOCK ONLY "
        "(appended after the per-source split), so every held-out block stays "
        "byte-identical to iteration 1 — the gate frames never move. "
        "(v4b's pre-split merge let aug rows land in test: methodology bug, "
        "corrected here and recorded in config.)",
    )
    ap.add_argument(
        "--tag",
        default="",
        help="output suffix so iteration records stay intact (e.g. 'v4b' -> metrics_v4b.json)",
    )
    args = ap.parse_args()

    exp_dir = Path(__file__).resolve().parent
    prod = pd.read_csv(PROD_V3)
    tight = pd.read_csv(TIGHT)
    span = pd.read_csv(SPAN)
    prod_v2 = pd.read_csv(PROD_V2)
    sources = {
        "prod_detection_window_v3": prod,
        "tight_adhoc_frame_v1": tight,
        "exact_span_frame": span,
    }
    blocks: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    for name, df in sources.items():
        df["frame_source"] = name
        df["window_end"] = pd.to_datetime(df["window_end"], utc=True)
        blocks[name] = temporal_split(df, 0.6, 0.2)
    prod_v2["window_end"] = pd.to_datetime(prod_v2["window_end"], utc=True)
    _, _, prod_v2_test = temporal_split(prod_v2, 0.6, 0.2)  # the exp05 gate block

    train = pd.concat([b[0] for b in blocks.values()], ignore_index=True)
    val = pd.concat([b[1] for b in blocks.values()], ignore_index=True)
    test = pd.concat([b[2] for b in blocks.values()], ignore_index=True)
    aug_note = None
    if args.aug_csv is not None:
        aug = pd.read_csv(args.aug_csv)
        aug["frame_source"] = "pure_sr_drop_aug"
        aug["window_end"] = pd.to_datetime(aug["window_end"], utc=True)
        # TRAIN-ONLY: held-out blocks stay byte-identical to iteration 1.
        train = pd.concat([train, aug], ignore_index=True)
        aug_note = (
            f"{args.aug_csv.name}: {len(aug)} rows TRAIN-ONLY (no held-out block "
            f"changes vs iteration 1); sha256 {_sha256(args.aug_csv)}"
        )
    df = pd.concat([train, val, test], ignore_index=True)

    # Demo frames computed once, shared by every candidate + the incumbent.
    print("computing demo frames (A: 140k events, B/D: 60k) ...", flush=True)
    demo_feats = {
        "A": _demo_frame(config_a(), 240),
        "B": _demo_frame(config_b(), 180),
        "D": _demo_frame(config_d(), 180),
    }

    labels = list(CAUSES)
    incumbent = load_active_artifact(BACKEND / "artifacts")
    assert incumbent is not None, "no active artifact to compare against"
    inc_est = incumbent["model"]
    incumbent_frames = {
        "prod_v3_test": _score(inc_est, blocks["prod_detection_window_v3"][2], labels),
        "tight_test": _score(inc_est, blocks["tight_adhoc_frame_v1"][2], labels),
        "span_test": _score(inc_est, blocks["exact_span_frame"][2], labels),
        "prod_v2_test_legacy": _score(inc_est, prod_v2_test, labels),
        "demo": _demo_score(inc_est, demo_feats),
    }
    inc_prod = incumbent_frames["prod_v3_test"]
    print(
        f"incumbent {incumbent['model_version']}: prod_v3 F1={inc_prod['macro_f1']:.4f} "
        f"safe={inc_prod['business']['safe_auto_lane_coverage']} "
        f"unsafe={inc_prod['business']['unsafe_coverage']} | "
        f"demoA={incumbent_frames['demo']['A']['gateway_degradation_conf']} "
        f"demoD={incumbent_frames['demo']['D']['gateway_degradation_conf']}",
        flush=True,
    )

    print("training 9 candidates on v4 ...", flush=True)
    result = train_and_compare(df, seed=SEED, presplit=(train, val, test))

    candidates: dict[str, dict] = {}
    algo_results: dict[str, AlgoResult] = result.algo_results
    for algo in ALGO_ORDER:
        for cal in CALIBRATION_MODES:
            name = candidate_name(algo, cal)
            r = algo_results[name]
            est = r.estimator
            prod_test_m = _score(est, blocks["prod_detection_window_v3"][2], labels)
            span_test_m = _score(est, blocks["exact_span_frame"][2], labels)
            demo = _demo_score(est, demo_feats)
            gate = {
                "prod_safe_better": (
                    prod_test_m["business"]["safe_auto_lane_coverage"] is not None
                    and inc_prod["business"]["safe_auto_lane_coverage"] is not None
                    and prod_test_m["business"]["safe_auto_lane_coverage"]
                    > inc_prod["business"]["safe_auto_lane_coverage"]
                ),
                "prod_unsafe_materially_lower": (
                    prod_test_m["business"]["unsafe_coverage"] is not None
                    and inc_prod["business"]["unsafe_coverage"] is not None
                    and prod_test_m["business"]["unsafe_coverage"]
                    <= round(inc_prod["business"]["unsafe_coverage"] - UNSAFE_MATERIAL_DROP, 4)
                ),
                "prod_f1_not_materially_worse": prod_test_m["macro_f1"]
                >= round(inc_prod["macro_f1"] - F1_MATERIAL_BAND, 4),
                "span_continuity": (
                    span_test_m["top1_accuracy"]
                    >= round(incumbent_frames["span_test"]["top1_accuracy"] - SPAN_CONTINUITY_BAND, 4)
                    and span_test_m["macro_f1"]
                    >= round(incumbent_frames["span_test"]["macro_f1"] - SPAN_CONTINUITY_BAND, 4)
                ),
                "demo_a": demo["A"]["pass"],
                "demo_d": demo["D"]["pass"],
            }
            gate["all"] = all(gate.values())
            candidates[name] = {
                "val": r.val_metrics,
                "prod_v3_test": prod_test_m,
                "tight_test": _score(est, blocks["tight_adhoc_frame_v1"][2], labels),
                "span_test": span_test_m,
                "prod_v2_test_legacy": _score(est, prod_v2_test, labels),
                "demo": demo,
                "gate": gate,
            }
            print(
                f"{name:<32} prod safe={prod_test_m['business']['safe_auto_lane_coverage']:>7} "
                f"unsafe={prod_test_m['business']['unsafe_coverage']:>7} "
                f"F1={prod_test_m['macro_f1']:.4f} | span top1={span_test_m['top1_accuracy']:.4f} "
                f"| A={demo['A']['gateway_degradation_conf']:.3f} D={demo['D']['gateway_degradation_conf']:.3f} "
                f"| gate={'PASS' if gate['all'] else 'fail'}",
                flush=True,
            )

    passers = [n for n, c in candidates.items() if c["gate"]["all"]]
    ship = None
    if passers:
        ship = select_candidate(
            {n: algo_results[n] for n in passers},
            [(a, c) for a in ALGO_ORDER for c in CALIBRATION_MODES if candidate_name(a, c) in passers],
        )

    config = {
        "experiment": "exp07_tight_frame_v4" + (f" iteration 2 ({args.tag})" if args.tag else ""),
        "script": str(Path(__file__).relative_to(REPO_ROOT)),
        "argv": sys.argv,
        "augmentation": aug_note,
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_sha": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=10,
        ).stdout.strip(),
        "dataset": {
            "version": "v4b" if args.aug_csv is not None else "v4",
            "rows": len(df),
            "sources": {
                name: {
                    "rows": len(sources[name]),
                    "sha256": _sha256(path),
                    "split": {k: len(b) for k, b in zip(("train", "val", "test"), blocks[name])},
                }
                for name, path in (
                    ("prod_detection_window_v3", PROD_V3),
                    ("tight_adhoc_frame_v1", TIGHT),
                    ("exact_span_frame", SPAN),
                )
            },
            "split_policy": "per-source temporal 0.6/0.2/0.2 (each family's test block stays exact)"
            + ("; augmentation rows TRAIN-ONLY (held-out blocks identical to iteration 1)" if args.aug_csv is not None else ""),
            "prod_frames_v2": "NOT trained on (contaminated shard2, exp04) — held-out legacy reference only",
        },
        "seed": SEED,
        "selection_rule": SELECTION_RULE,
        "gate": {
            "clauses": [
                f"prod_v3 test: safe strictly > incumbent, unsafe <= incumbent - {UNSAFE_MATERIAL_DROP}, "
                f"macro-F1 >= incumbent - {F1_MATERIAL_BAND}",
                f"span test: top-1 and macro-F1 >= incumbent - {SPAN_CONTINUITY_BAND}",
                f"demo A gateway_degradation conf >= {DEMO_FLOORS['A']} (top-1), "
                f"demo D >= {DEMO_FLOORS['D']} (top-1)",
                "tests/demo 10/10 + full backend suite green after the swap (post-check, rollback on fail)",
            ],
            "incumbent": incumbent["model_version"],
        },
        "val_rule_winner": result.best_algo,
        "ship_candidate": ship,
    }
    metrics = {
        "incumbent": {
            "model_version": incumbent["model_version"],
            **incumbent_frames,
        },
        "candidates": candidates,
        "gate_passers": passers,
        "ship_candidate": ship,
        "val_rule_winner_overall": result.best_algo,
    }
    suffix = f"_{args.tag}" if args.tag else ""
    (exp_dir / f"config{suffix}.json").write_text(json.dumps(config, indent=2, default=str) + "\n", encoding="utf-8")
    (exp_dir / f"metrics{suffix}.json").write_text(json.dumps(metrics, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"\nval-rule winner (overall): {result.best_algo}")
    print(f"gate passers: {passers or 'NONE'}")
    print(f"ship candidate: {ship or 'NO-SHIP'}")
    print(f"wrote {exp_dir / f'metrics{suffix}.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

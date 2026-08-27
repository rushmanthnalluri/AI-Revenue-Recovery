"""Train and honestly compare diagnosis root-cause models.

Run from backend/:

    # Standalone today: mini synthetic generator mirrors the simulator taxonomy
    python scripts/train_models.py --synthetic --windows-per-class 200

    # Simulator output (precomputed feature rows), e.g. the production-frame
    # dataset built by ml/experiments/diagnosis/exp01_prod_frames_dataset:
    python scripts/train_models.py --input artifacts/prod_frames_v1.csv \
        --experiment-dir ../ml/experiments/diagnosis/exp03_calibration_candidates

What it does: temporal train/val/test split (no leakage) -> fit every
(algorithm x calibration) candidate — logistic regression (baseline), random
forest, gradient boosting, each raw and CalibratedClassifierCV(sigmoid |
isotonic) with TIME-AWARE calibration CV — -> select on the pre-registered
validation rule (training.SELECTION_RULE: safe auto-lane coverage, macro-F1,
ECE) -> report once on the held-out test block -> joblib artifact + active
pointer in backend/artifacts/ -> experiment + test-set predictions persisted
to the database (unless --skip-db).

Input frame contract (CSV/parquet): one row per incident window; columns =
the diagnosis FEATURE_NAMES + ``label`` (taxonomy value) + ``window_end``
(sortable timestamp; drives the temporal split) + optional window metadata.

``--experiment-dir`` writes a reproduction-grade record: config.json (full
CLI config, dataset version incl. sha256, feature version, git sha, selection
rule), metrics.json (every candidate's full val+test metric set), and
confusion_matrix.csv (selected candidate, test block).
"""

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

import pandas as pd

from app.services.diagnosis.features import FEATURE_NAMES
from app.services.diagnosis.synthetic import SyntheticConfig, generate_dataset
from app.services.diagnosis.training import (
    CALIBRATION_MODES,
    SELECTION_RULE,
    load_active_artifact,
    persist_test_predictions,
    persist_training_run,
    save_artifacts,
    train_and_compare,
)

DEFAULT_ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "artifacts"

logger = logging.getLogger("train_models")

#: Feature-contract version: content digest of the 58-feature names, so
#: experiment records pin the exact contract the numbers were produced under.
FEATURE_VERSION = hashlib.sha256(",".join(FEATURE_NAMES).encode()).hexdigest()[:8]


def load_frame(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(path)
    elif suffix in (".parquet", ".pq"):
        try:
            df = pd.read_parquet(path)
        except ImportError as exc:  # no parquet engine in the pinned deps
            raise SystemExit(
                "reading parquet requires pyarrow or fastparquet, which is not in "
                "requirements.txt — export CSV instead, or install an engine in "
                "your local venv (do not commit the dependency change)."
            ) from exc
    else:
        raise SystemExit(f"unsupported input format {suffix!r} (expected .csv/.parquet)")
    for col in ("window_start", "window_end"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True)
    return df


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[2],
            timeout=10,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # git unavailable — record honestly
        return "unknown"


def write_experiment_record(
    experiment_dir: Path,
    *,
    args: argparse.Namespace,
    dataset_desc: str,
    dataset_version: dict,
    result,
) -> None:
    """Write config.json / metrics.json / confusion_matrix.csv for the run."""
    experiment_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "script": "backend/scripts/train_models.py",
        "argv": sys.argv,
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "dataset": dataset_version,
        "feature_version": FEATURE_VERSION,
        "feature_count": len(FEATURE_NAMES),
        "model_version": result.model_version,
        "seed": args.seed,
        "train_frac": args.train_frac,
        "val_frac": args.val_frac,
        "calibrations": list(args.calibrations),
        "selection_rule": SELECTION_RULE,
        "split": result.split,
    }
    (experiment_dir / "config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )
    metrics = {
        "dataset_desc": dataset_desc,
        "model_version": result.model_version,
        "selected_candidate": result.best_algo,
        "selection_rule": SELECTION_RULE,
        "candidates": {
            name: {"val": r.val_metrics, "test": r.test_metrics}
            for name, r in result.algo_results.items()
        },
    }
    (experiment_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )
    cm = result.best.test_metrics["confusion_matrix"]
    lines = ["true\\predicted," + ",".join(cm["labels"])]
    for label, row in zip(cm["labels"], cm["matrix"]):
        lines.append(label + "," + ",".join(str(v) for v in row))
    (experiment_dir / "confusion_matrix.csv").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    logger.info("wrote experiment record to %s", experiment_dir)


def print_summary(result, dataset_desc: str) -> None:
    print(f"\nDataset: {dataset_desc}")
    split = result.split
    print(
        "Temporal split (by window_end, no shuffle): "
        f"train={split['counts']['train']} val={split['counts']['val']} test={split['counts']['test']}"
    )
    header = (
        f"{'candidate':<32} {'val F1':>7} {'val safe':>8} {'val ECE':>7} | "
        f"{'test F1':>7} {'test top1':>9} {'test top3':>9} {'test safe':>9} {'test ECE':>8}"
    )
    print("\n" + header + "\n" + "-" * len(header))
    for name, r in result.algo_results.items():
        marker = "  <- selected" if name == result.best_algo else ""
        v_safe = r.val_metrics["business"]["safe_auto_lane_coverage"]
        t_safe = r.test_metrics["business"]["safe_auto_lane_coverage"]
        print(
            f"{name:<32} {r.val_metrics['macro_f1']:>7.4f} "
            f"{v_safe if v_safe is not None else float('nan'):>8.4f} {r.val_metrics['ece']:>7.4f} | "
            f"{r.test_metrics['macro_f1']:>7.4f} {r.test_metrics['top1_accuracy']:>9.4f} "
            f"{r.test_metrics['top3_accuracy']:>9.4f} "
            f"{t_safe if t_safe is not None else float('nan'):>9.4f} {r.test_metrics['ece']:>8.4f}{marker}"
        )
    best = result.best.test_metrics
    print(f"\nmodel_version: {result.model_version}")
    print(f"selection rule: {SELECTION_RULE}")
    print(f"selected model per-class test metrics ({result.best_algo}):")
    for label, m in best["per_class"].items():
        print(
            f"  {label:<36} P={m['precision']:.3f} R={m['recall']:.3f} "
            f"F1={m['f1']:.3f} support={m['support']}"
        )
    if best["top_confusions"]:
        top = best["top_confusions"][0]
        print(f"top confusion: {top['true']} -> {top['predicted']} ({top['count']} rows)")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--input", type=Path, help="CSV/parquet of precomputed feature rows")
    src.add_argument("--synthetic", action="store_true", help="generate the mini synthetic dataset (default)")
    p.add_argument("--windows-per-class", type=int, default=200)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--train-frac", type=float, default=0.6)
    p.add_argument("--val-frac", type=float, default=0.2)
    p.add_argument(
        "--calibrations",
        type=lambda s: tuple(s.split(",")),
        default=CALIBRATION_MODES,
        help="comma-separated subset of none,sigmoid,isotonic (default: all three)",
    )
    p.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    p.add_argument("--emit-csv", type=Path, default=None, help="also write the training frame here")
    p.add_argument("--skip-db", action="store_true", help="do not persist experiment/predictions to the DB")
    p.add_argument(
        "--experiment-dir",
        type=Path,
        default=None,
        help="write config.json / metrics.json / confusion_matrix.csv here",
    )
    args = p.parse_args()

    dataset_version: dict
    if args.input:
        df = load_frame(args.input)
        dataset_version = {
            "input": str(args.input),
            "rows": len(df),
            "sha256": _file_sha256(args.input),
            "label_distribution": {
                str(k): int(v) for k, v in df["label"].value_counts().items()
            },
        }
        dataset_desc = f"input:{args.input} rows={len(df)}"
    else:
        cfg = SyntheticConfig(windows_per_class=args.windows_per_class, seed=args.seed)
        df = generate_dataset(cfg)
        dataset_version = {
            "synthetic_mini": True,
            "windows_per_class": args.windows_per_class,
            "rows": len(df),
            "seed": args.seed,
        }
        dataset_desc = (
            f"synthetic-mini windows_per_class={args.windows_per_class} seed={args.seed} rows={len(df)}"
        )
        logger.info("generated %d synthetic windows", len(df))

    if args.emit_csv:
        args.emit_csv.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.emit_csv, index=False)
        logger.info("wrote training frame to %s", args.emit_csv)

    result = train_and_compare(
        df,
        seed=args.seed,
        train_frac=args.train_frac,
        val_frac=args.val_frac,
        calibrations=args.calibrations,
    )
    artifact_path = save_artifacts(result, args.artifacts_dir, dataset_desc)

    if args.experiment_dir:
        write_experiment_record(
            args.experiment_dir,
            args=args,
            dataset_desc=dataset_desc,
            dataset_version=dataset_version,
            result=result,
        )

    if not args.skip_db:
        # Standalone-today convenience: ensure tables exist in the default
        # SQLite file DB (idempotent; alembic remains the migration owner).
        import app.models  # noqa: F401  (register tables)
        from app.db import Base, SessionLocal, engine

        Base.metadata.create_all(engine)
        with SessionLocal() as session:
            exp = persist_training_run(session, result, dataset_desc)
            n_pred = persist_test_predictions(session, result)
            session.commit()
            print(f"\npersisted experiment {exp.id} and {n_pred} test-set model_predictions rows")
    else:
        print("\n--skip-db: no experiment/prediction rows written")

    print_summary(result, dataset_desc)
    print(f"\nartifact: {artifact_path}")
    active = load_active_artifact(args.artifacts_dir)
    assert active is not None and active["model_version"] == result.model_version
    print(f"active pointer OK -> {result.best_algo} @ {result.model_version}")


if __name__ == "__main__":
    main()

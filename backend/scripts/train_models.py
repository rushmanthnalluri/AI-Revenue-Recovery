"""Train and honestly compare diagnosis root-cause models.

Run from backend/:

    # Standalone today: mini synthetic generator mirrors the simulator taxonomy
    python scripts/train_models.py --synthetic --windows-per-class 200

    # Later wave: full simulator output (precomputed feature rows)
    python scripts/train_models.py --input path/to/features.csv
    python scripts/train_models.py --input path/to/features.parquet

What it does: temporal train/val/test split (no leakage) -> fit logistic
regression (baseline), random forest, gradient boosting -> select on
validation macro-F1 -> report once on the held-out test block -> joblib
artifact + active pointer in backend/artifacts/ -> experiment + test-set
predictions persisted to the database (unless --skip-db).

Input frame contract (CSV/parquet): one row per incident window; columns =
the diagnosis FEATURE_NAMES + ``label`` (taxonomy value) + ``window_end``
(sortable timestamp; drives the temporal split) + optional window metadata.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

import pandas as pd

from app.services.diagnosis.synthetic import SyntheticConfig, generate_dataset
from app.services.diagnosis.training import (
    ALGO_ORDER,
    load_active_artifact,
    persist_test_predictions,
    persist_training_run,
    save_artifacts,
    train_and_compare,
)

DEFAULT_ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "artifacts"

logger = logging.getLogger("train_models")


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


def print_summary(result, dataset_desc: str) -> None:
    print(f"\nDataset: {dataset_desc}")
    split = result.split
    print(
        "Temporal split (by window_end, no shuffle): "
        f"train={split['counts']['train']} val={split['counts']['val']} test={split['counts']['test']}"
    )
    header = f"{'algo':<22} {'val F1':>7} {'val top1':>8} | {'test F1':>7} {'test top1':>9} {'test top3':>9}"
    print("\n" + header + "\n" + "-" * len(header))
    for algo in ALGO_ORDER:
        r = result.algo_results[algo]
        marker = "  <- selected" if algo == result.best_algo else ""
        print(
            f"{algo:<22} {r.val_metrics['macro_f1']:>7.4f} {r.val_metrics['top1_accuracy']:>8.4f} | "
            f"{r.test_metrics['macro_f1']:>7.4f} {r.test_metrics['top1_accuracy']:>9.4f} "
            f"{r.test_metrics['top3_accuracy']:>9.4f}{marker}"
        )
    best = result.best.test_metrics
    print(f"\nmodel_version: {result.model_version}")
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
    p.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    p.add_argument("--emit-csv", type=Path, default=None, help="also write the training frame here")
    p.add_argument("--skip-db", action="store_true", help="do not persist experiment/predictions to the DB")
    args = p.parse_args()

    if args.input:
        df = load_frame(args.input)
        dataset_desc = f"input:{args.input} rows={len(df)}"
    else:
        cfg = SyntheticConfig(windows_per_class=args.windows_per_class, seed=args.seed)
        df = generate_dataset(cfg)
        dataset_desc = (
            f"synthetic-mini windows_per_class={args.windows_per_class} seed={args.seed} rows={len(df)}"
        )
        logger.info("generated %d synthetic windows", len(df))

    if args.emit_csv:
        args.emit_csv.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.emit_csv, index=False)
        logger.info("wrote training frame to %s", args.emit_csv)

    result = train_and_compare(df, seed=args.seed, train_frac=args.train_frac, val_frac=args.val_frac)
    artifact_path = save_artifacts(result, args.artifacts_dir, dataset_desc)

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

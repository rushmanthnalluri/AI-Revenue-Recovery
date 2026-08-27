"""Training pipeline: temporal split, baseline-first model comparison, honest
metrics, artifact persistence, and experiment tracking.

Split logic (leakage control)
-----------------------------
Rows are incident windows with a ``window_end`` timestamp. The frame is sorted
by ``window_end`` and cut into contiguous blocks: first ``train_frac`` train,
next ``val_frac`` validation, remainder test. **No shuffling.** Future windows
never influence the fit or the model selection; each window's features are
computed only from data inside the window and its immediately preceding
baseline window, so no row can contain information from another row's future.

Model selection: macro-F1 on the *validation* block. Reported headline numbers
are computed once, on the untouched *test* block, with the val-selected model
(no refit on train+val — keeps the test estimate honest and the code simple).

Baselines first: multinomial logistic regression (scaled) is the interpretable
baseline; random forest and gradient boosting must beat it on validation
macro-F1 to be selected. Whatever wins is what ``diagnosis_active.json``
points at — the comparison is real, not ceremonial.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    precision_recall_fscore_support,
    top_k_accuracy_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy.orm import Session

from app.models import Experiment, ModelPrediction
from app.services.diagnosis.features import FEATURE_NAMES, features_to_vector
from app.services.diagnosis.taxonomy import CAUSES

logger = logging.getLogger(__name__)

#: Algorithm zoo, in baseline-first order. Ties on validation macro-F1 resolve
#: to the earlier (simpler) entry.
ALGO_ORDER = ("logistic_regression", "random_forest", "gradient_boosting")


def build_estimator(algo: str, seed: int) -> Any:
    if algo == "logistic_regression":
        return Pipeline(
            [
                ("scale", StandardScaler()),
                (
                    "clf",
                    LogisticRegression(max_iter=2000, C=1.0, class_weight="balanced"),
                ),
            ]
        )
    if algo == "random_forest":
        return RandomForestClassifier(
            n_estimators=200,
            min_samples_leaf=2,
            class_weight="balanced_subsample",
            random_state=seed,
            n_jobs=-1,
        )
    if algo == "gradient_boosting":
        return GradientBoostingClassifier(random_state=seed)
    raise ValueError(f"unknown algo {algo!r}")


# ---------------------------------------------------------------------------
# Temporal split
# ---------------------------------------------------------------------------

def temporal_split(
    df: pd.DataFrame,
    train_frac: float = 0.6,
    val_frac: float = 0.2,
    time_col: str = "window_end",
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Contiguous time-ordered train/val/test blocks. No shuffling, so the
    three blocks are disjoint in time: max(train.t) <= min(val.t) etc."""
    if not (0.0 < train_frac < 1.0) or not (0.0 <= val_frac < 1.0) or train_frac + val_frac >= 1.0:
        raise ValueError(f"bad split fractions: train={train_frac} val={val_frac}")
    ordered = df.sort_values(time_col, kind="mergesort").reset_index(drop=True)
    n = len(ordered)
    n_train = int(n * train_frac)
    n_val = int(n * val_frac)
    train = ordered.iloc[:n_train].copy()
    val = ordered.iloc[n_train : n_train + n_val].copy()
    test = ordered.iloc[n_train + n_val :].copy()
    return train, val, test


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    proba: np.ndarray,
    proba_classes: np.ndarray,
    labels: list[str],
) -> dict[str, Any]:
    """Per-class P/R/F1, top-1/top-3 accuracy, and a confusion summary."""
    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=labels, zero_division=0
    )
    per_class = {
        label: {
            "precision": round(float(precision[i]), 4),
            "recall": round(float(recall[i]), 4),
            "f1": round(float(f1[i]), 4),
            "support": int(support[i]),
        }
        for i, label in enumerate(labels)
    }
    macro_f1 = float(np.mean([per_class[l]["f1"] for l in labels]))
    top1 = float(accuracy_score(y_true, y_pred))

    # Align probability columns to a sorted label order (top_k_accuracy_score
    # requires `labels` to be ordered).
    sorted_labels = sorted(labels)
    aligned = np.zeros((len(y_true), len(labels)))
    for j, cls in enumerate(proba_classes):
        aligned[:, sorted_labels.index(str(cls))] = proba[:, j]
    top3 = float(
        top_k_accuracy_score(y_true, aligned, k=min(3, len(labels)), labels=sorted_labels)
    )

    cm = confusion_matrix(y_true, y_pred, labels=labels)
    confusions = []
    for i, t in enumerate(labels):
        for j, p in enumerate(labels):
            if i != j and cm[i, j] > 0:
                confusions.append({"true": t, "predicted": p, "count": int(cm[i, j])})
    confusions.sort(key=lambda c: c["count"], reverse=True)

    return {
        "per_class": per_class,
        "macro_f1": round(macro_f1, 4),
        "top1_accuracy": round(top1, 4),
        "top3_accuracy": round(top3, 4),
        "n_samples": int(len(y_true)),
        "confusion_matrix": {"labels": labels, "matrix": cm.tolist()},
        "top_confusions": confusions[:5],
    }


# ---------------------------------------------------------------------------
# Training driver
# ---------------------------------------------------------------------------

@dataclass
class AlgoResult:
    algo: str
    estimator: Any
    val_metrics: dict[str, Any]
    test_metrics: dict[str, Any]


@dataclass
class TrainingResult:
    model_version: str
    trained_at: str
    labels: list[str]
    feature_names: list[str]
    split: dict[str, Any]
    algo_results: dict[str, AlgoResult]
    best_algo: str
    test_features: list[dict[str, float]] = field(default_factory=list)
    test_true: list[str] = field(default_factory=list)
    test_pred: list[str] = field(default_factory=list)
    test_proba: list[dict[str, float]] = field(default_factory=list)

    @property
    def best(self) -> AlgoResult:
        return self.algo_results[self.best_algo]


def make_model_version(df: pd.DataFrame, seed: int) -> str:
    digest = hashlib.sha256(
        f"{len(df)}|{seed}|{','.join(FEATURE_NAMES)}|{','.join(CAUSES)}".encode()
    ).hexdigest()[:8]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"v{stamp}-{digest}"


def validate_frame(df: pd.DataFrame) -> list[str]:
    """Check the training frame contract; returns the label list (canonical
    order, restricted to labels actually present)."""
    missing = [c for c in ("label", "window_end", *FEATURE_NAMES) if c not in df.columns]
    if missing:
        raise ValueError(f"training frame missing columns: {missing[:5]}... ({len(missing)} total)")
    labels = [c for c in CAUSES if c in set(df["label"])]
    unknown = set(df["label"]) - set(CAUSES)
    if unknown:
        raise ValueError(f"unknown labels in training frame: {sorted(unknown)}")
    if len(labels) < 2:
        raise ValueError("need at least two classes to train a classifier")
    return labels


def train_and_compare(
    df: pd.DataFrame,
    *,
    seed: int = 42,
    train_frac: float = 0.6,
    val_frac: float = 0.2,
) -> TrainingResult:
    """Fit all algorithms, select on validation macro-F1, report on test."""
    labels = validate_frame(df)
    train, val, test = temporal_split(df, train_frac, val_frac)

    def xy(block: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        return (
            np.asarray([features_to_vector(r) for r in block[FEATURE_NAMES].to_dict("records")]),
            block["label"].to_numpy(),
        )

    X_train, y_train = xy(train)
    X_val, y_val = xy(val)
    X_test, y_test = xy(test)

    algo_results: dict[str, AlgoResult] = {}
    for algo in ALGO_ORDER:
        est = build_estimator(algo, seed)
        est.fit(X_train, y_train)
        val_metrics = compute_metrics(y_val, est.predict(X_val), est.predict_proba(X_val), est.classes_, labels)
        test_metrics = compute_metrics(y_test, est.predict(X_test), est.predict_proba(X_test), est.classes_, labels)
        algo_results[algo] = AlgoResult(algo, est, val_metrics, test_metrics)
        logger.info(
            "trained %s: val macro-F1=%.4f top1=%.4f | test macro-F1=%.4f top1=%.4f top3=%.4f",
            algo,
            val_metrics["macro_f1"],
            val_metrics["top1_accuracy"],
            test_metrics["macro_f1"],
            test_metrics["top1_accuracy"],
            test_metrics["top3_accuracy"],
        )

    # Baseline-first tie-break: iterate ALGO_ORDER, keep first maximal val F1.
    best_algo = max(ALGO_ORDER, key=lambda a: (algo_results[a].val_metrics["macro_f1"], -ALGO_ORDER.index(a)))
    best = algo_results[best_algo]

    version = make_model_version(df, seed)
    split_info = {
        "policy": "temporal contiguous blocks sorted by window_end; no shuffling",
        "train_frac": train_frac,
        "val_frac": val_frac,
        "counts": {"train": len(train), "val": len(val), "test": len(test)},
        "ranges": {
            name: {
                "start": str(block["window_end"].min()),
                "end": str(block["window_end"].max()),
            }
            for name, block in (("train", train), ("val", val), ("test", test))
        },
    }

    proba_test = best.estimator.predict_proba(X_test)
    classes = [str(c) for c in best.estimator.classes_]
    test_features = [
        {name: float(v) for name, v in zip(FEATURE_NAMES, row)} for row in X_test
    ]
    test_proba = [
        {classes[j]: round(float(row[j]), 6) for j in range(len(classes))} for row in proba_test
    ]

    return TrainingResult(
        model_version=version,
        trained_at=datetime.now(timezone.utc).isoformat(),
        labels=labels,
        feature_names=list(FEATURE_NAMES),
        split=split_info,
        algo_results=algo_results,
        best_algo=best_algo,
        test_features=test_features,
        test_true=[str(t) for t in y_test],
        test_pred=[str(p) for p in best.estimator.predict(X_test)],
        test_proba=test_proba,
    )


# ---------------------------------------------------------------------------
# Artifact persistence
# ---------------------------------------------------------------------------

ACTIVE_POINTER = "diagnosis_active.json"


def save_artifacts(result: TrainingResult, artifacts_dir: Path, dataset_desc: str) -> Path:
    """Persist the winning pipeline via joblib plus a small JSON pointer that
    marks the active model for DiagnosisService."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    name = f"diagnosis_{result.best_algo}_{result.model_version}.joblib"
    path = artifacts_dir / name
    payload = {
        "model": result.best.estimator,
        "algo": result.best_algo,
        "model_version": result.model_version,
        "trained_at": result.trained_at,
        "feature_names": result.feature_names,
        "labels": result.labels,
        "dataset": dataset_desc,
        "sklearn_version": sklearn.__version__,
        "metrics": {
            "val": result.best.val_metrics,
            "test": result.best.test_metrics,
        },
    }
    joblib.dump(payload, path)
    pointer = {
        "artifact": name,
        "algo": result.best_algo,
        "model_version": result.model_version,
        "trained_at": result.trained_at,
        "dataset": dataset_desc,
        "val_macro_f1": result.best.val_metrics["macro_f1"],
        "test_macro_f1": result.best.test_metrics["macro_f1"],
        "test_top1_accuracy": result.best.test_metrics["top1_accuracy"],
        "test_top3_accuracy": result.best.test_metrics["top3_accuracy"],
    }
    (artifacts_dir / ACTIVE_POINTER).write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")
    logger.info("saved artifact %s (active pointer updated)", path)
    return path


def load_active_artifact(artifacts_dir: Path) -> dict[str, Any] | None:
    """Load the artifact named by the active pointer; None when absent."""
    pointer_path = artifacts_dir / ACTIVE_POINTER
    if not pointer_path.exists():
        return None
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    artifact_path = artifacts_dir / pointer["artifact"]
    if not artifact_path.exists():
        logger.warning("active pointer references missing artifact %s", artifact_path)
        return None
    return joblib.load(artifact_path)


# ---------------------------------------------------------------------------
# Experiment / prediction persistence (shared models, write-only for us)
# ---------------------------------------------------------------------------

def persist_training_run(session: Session, result: TrainingResult, dataset_desc: str) -> Experiment:
    """Store the full comparison (all algos, val+test) on an experiments row."""
    exp = Experiment(
        name=f"diagnosis-training-{result.model_version}",
        description="Diagnosis root-cause model comparison (LR vs RF vs GB, temporal split)",
        status="completed",
        started_at=datetime.fromisoformat(result.trained_at),
        ended_at=datetime.fromisoformat(result.trained_at),
        config={
            "algos": list(ALGO_ORDER),
            "seed_dataset": dataset_desc,
            "feature_count": len(result.feature_names),
            "labels": result.labels,
            "split": result.split,
        },
        results={
            "model_version": result.model_version,
            "best_algo": result.best_algo,
            "selection": "max validation macro_f1, ties to the simpler baseline",
            "algos": {
                algo: {"val": r.val_metrics, "test": r.test_metrics}
                for algo, r in result.algo_results.items()
            },
        },
    )
    session.add(exp)
    return exp


def persist_test_predictions(session: Session, result: TrainingResult) -> int:
    """Persist every test-set prediction of the winning model to
    model_predictions (auditability + offline evaluation material)."""
    rows = 0
    for feats, true_label, pred_label, proba in zip(
        result.test_features, result.test_true, result.test_pred, result.test_proba
    ):
        session.add(
            ModelPrediction(
                incident_id=None,
                model_name=f"diagnosis-{result.best_algo}",
                model_version=result.model_version,
                prediction_type="diagnosis",
                input_features=feats,
                output={
                    "label": pred_label,
                    "true_label": true_label,
                    "correct": pred_label == true_label,
                    "proba": proba,
                    "split": "test",
                    "heuristic": False,
                },
                score=proba.get(pred_label),
            )
        )
        rows += 1
    return rows


__all__ = [
    "ALGO_ORDER",
    "AlgoResult",
    "TrainingResult",
    "ACTIVE_POINTER",
    "build_estimator",
    "temporal_split",
    "compute_metrics",
    "validate_frame",
    "train_and_compare",
    "save_artifacts",
    "load_active_artifact",
    "persist_training_run",
    "persist_test_predictions",
]

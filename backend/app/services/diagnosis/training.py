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
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    precision_recall_fscore_support,
    top_k_accuracy_score,
)
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy.orm import Session

from app.models import Experiment, ModelPrediction
from app.services.diagnosis.features import FEATURE_NAMES, features_to_vector
from app.services.diagnosis.taxonomy import AUTO_RECOVERABLE_CAUSES, CAUSES

logger = logging.getLogger(__name__)

#: Algorithm zoo, in baseline-first order. Ties on validation macro-F1 resolve
#: to the earlier (simpler) entry.
ALGO_ORDER = ("logistic_regression", "random_forest", "gradient_boosting")

#: Calibration modes compared for every algorithm. ``none`` is the raw
#: estimator; ``sigmoid`` (Platt) and ``isotonic`` wrap the base estimator in
#: ``CalibratedClassifierCV``. The calibration CV is TIME-AWARE
#: (``TimeSeriesSplit`` over the chronologically-ordered training block):
#: calibration maps are always fit on data in the fold's future-free past and
#: scored on its future, so no validation/test information — and no future
#: training information — leaks into the calibration. Calibration is a
#: financial-safety property here: strategy confidence = diagnosis confidence
#: x action-fit, gated at 0.85 for auto-execute (policies/default.yaml), so an
#: overconfident model is an unsafe-automation risk, not a cosmetic issue.
CALIBRATION_MODES = ("none", "sigmoid", "isotonic")

#: Folds for the time-aware calibration CV (chronological, expanding window).
CALIBRATION_CV_SPLITS = 3

#: Auto-execute confidence floor from policies/default.yaml (min_confidence);
#: strategy confidence = diagnosis confidence x action_fit (<= 0.98), so a
#: diagnosis confidence >= this threshold is a NECESSARY condition for the
#: auto lane — which is what the business metric scores.
AUTO_EXECUTE_THRESHOLD = 0.85

#: Pre-registered candidate selection rule (validation block ONLY; the test
#: block is touched once, afterwards, by the selected candidate and the
#: shipped baseline it must beat):
#:   1. highest validation safe_auto_lane_coverage (business metric; ties at
#:      2 decimals),
#:   2. highest validation macro-F1 (ties at 3 decimals),
#:   3. lowest validation ECE (calibration),
#:   4. earliest candidate in candidate order (LR before RF before GB; raw
#:      before sigmoid before isotonic).
#: Business-first because the product objective is SAFE recovery of
#: recoverable revenue, not accuracy; macro-F1 second guards against a
#: degenerate never-confident winner; ECE third prefers the better-calibrated
#: of otherwise-equal candidates; simplicity last.
SELECTION_RULE = (
    "validation block only: max safe_auto_lane_coverage (ties at 2dp) -> "
    "max macro-F1 (ties at 3dp) -> min ECE -> earliest candidate order "
    "(LR>RF>GB, raw>sigmoid>isotonic)"
)


def candidate_name(algo: str, calibration: str) -> str:
    """Candidate id, e.g. ``random_forest+sigmoid`` (raw keeps the bare algo)."""
    return algo if calibration == "none" else f"{algo}+{calibration}"


def build_estimator(algo: str, seed: int, calibration: str = "none") -> Any:
    base = _base_estimator(algo, seed)
    if calibration == "none":
        return base
    if calibration not in ("sigmoid", "isotonic"):
        raise ValueError(f"unknown calibration {calibration!r}")
    return CalibratedClassifierCV(
        estimator=base,
        method=calibration,
        cv=TimeSeriesSplit(n_splits=CALIBRATION_CV_SPLITS),
    )


def _base_estimator(algo: str, seed: int) -> Any:
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

def expected_calibration_error(
    confidence: np.ndarray, correct: np.ndarray, n_bins: int = 10
) -> float:
    """ECE on the top-label confidence: equal-width bins over [0, 1],
    sum_b (n_b / N) * |accuracy_b - mean_confidence_b|. Empty bins skipped."""
    confidence = np.asarray(confidence, dtype=float)
    correct = np.asarray(correct, dtype=float)
    n = len(confidence)
    if n == 0:
        return 0.0
    ece = 0.0
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    # bin index in [0, n_bins-1]; confidence == 1.0 lands in the last bin
    idx = np.clip(np.digitize(confidence, edges[1:-1]), 0, n_bins - 1)
    for b in range(n_bins):
        mask = idx == b
        if not mask.any():
            continue
        ece += (mask.sum() / n) * abs(float(correct[mask].mean()) - float(confidence[mask].mean()))
    return float(ece)


def multiclass_brier(y_true: np.ndarray, aligned_proba: np.ndarray, labels: list[str]) -> float:
    """Multiclass Brier score: mean_i sum_k (p_ik - 1[y_i = k])^2 over the full
    label set (range [0, 2]; lower is better)."""
    index = {label: k for k, label in enumerate(labels)}
    onehot = np.zeros((len(y_true), len(labels)))
    for i, y in enumerate(y_true):
        onehot[i, index[str(y)]] = 1.0
    return float(np.mean(np.sum((aligned_proba - onehot) ** 2, axis=1)))


def macro_fpr(y_true: np.ndarray, y_pred: np.ndarray, labels: list[str]) -> float:
    """Macro one-vs-rest false-positive rate: mean over classes of
    FP / (FP + TN) (0 when a class has no negatives)."""
    fprs = []
    for label in labels:
        negatives = y_true != label
        tn = int(np.sum((y_pred != label) & negatives))
        fp = int(np.sum((y_pred == label) & negatives))
        fprs.append(fp / (fp + tn) if (fp + tn) > 0 else 0.0)
    return float(np.mean(fprs)) if fprs else 0.0


def safe_auto_lane_coverage(
    y_true: np.ndarray,
    confidence: np.ndarray,
    y_pred: np.ndarray | None = None,
    *,
    threshold: float = AUTO_EXECUTE_THRESHOLD,
    auto_classes: frozenset[str] = AUTO_RECOVERABLE_CAUSES,
) -> dict[str, Any]:
    """The business metric — SAFE recovery of genuinely recoverable revenue.

    A row *enters the auto lane* when its diagnosis confidence (max class
    probability) is >= ``threshold`` (0.85 — the policy auto-execute floor;
    strategy confidence = diagnosis confidence x action-fit <= 0.98 makes
    this a necessary condition, so the diagnosis-level rate is the honest
    upper bound the gate will ever see).

        auto_coverage   = P(enters auto lane | true class is auto-recoverable)
        unsafe_coverage = P(enters auto lane | true class is NOT auto-recoverable)
        safe_auto_lane_coverage = auto_coverage - unsafe_coverage   in [-1, 1]

    +1 means every genuinely recoverable incident and ONLY those cross the
    auto-execute floor. The unsafe side is deliberately strict: it prices ANY
    >=threshold confidence on a non-recoverable incident, because a confident
    output is what downstream automation consumes — a hedged correct answer
    never auto-executes, a confident one can.

    Also reported (diagnostic, not the selection metric):
    ``false_fire_rate`` = P(enters auto lane AND predicted class is
    auto-recoverable | true class is NOT) — the subset of the unsafe side
    that maps directly onto auto-executable retry/route-around strategies
    (the truly dangerous confusion, e.g. no_fault -> gateway_degradation at
    0.9 confidence).
    """
    y_true = np.asarray([str(y) for y in y_true])
    confidence = np.asarray(confidence, dtype=float)
    crosses = confidence >= threshold
    is_auto = np.asarray([y in auto_classes for y in y_true])
    n_auto = int(is_auto.sum())
    n_not = int((~is_auto).sum())
    auto_cov = float(np.mean(crosses[is_auto])) if n_auto else None
    unsafe_cov = float(np.mean(crosses[~is_auto])) if n_not else None
    # safe is computed from the REPORTED (rounded) components so the record is
    # internally consistent: safe == auto_coverage - unsafe_coverage exactly.
    safe = (
        round(round(auto_cov, 4) - round(unsafe_cov, 4), 4)
        if auto_cov is not None and unsafe_cov is not None
        else None
    )
    false_fire = None
    if y_pred is not None and n_not:
        pred_auto = np.asarray([str(p) in auto_classes for p in y_pred])
        false_fire = round(float(np.mean(crosses[~is_auto] & pred_auto[~is_auto])), 4)
    return {
        "threshold": threshold,
        "auto_classes": sorted(auto_classes),
        "n_auto_recoverable": n_auto,
        "n_not_auto_recoverable": n_not,
        "auto_coverage": round(auto_cov, 4) if auto_cov is not None else None,
        "unsafe_coverage": round(unsafe_cov, 4) if unsafe_cov is not None else None,
        "false_fire_rate": false_fire,
        "safe_auto_lane_coverage": safe,
    }


def align_proba(
    proba: np.ndarray, proba_classes: np.ndarray, labels: list[str]
) -> np.ndarray:
    """Align a predict_proba matrix's columns to ``labels`` order (missing
    classes get 0.0)."""
    aligned = np.zeros((proba.shape[0], len(labels)))
    index = {label: k for k, label in enumerate(labels)}
    for j, cls in enumerate(proba_classes):
        aligned[:, index[str(cls)]] = proba[:, j]
    return aligned


def compute_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    proba: np.ndarray,
    proba_classes: np.ndarray,
    labels: list[str],
) -> dict[str, Any]:
    """Per-class P/R/F1, macro-FPR, top-1/top-3, calibration (ECE + Brier),
    the safe-auto-lane business metric, and a confusion summary."""
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

    aligned = align_proba(proba, proba_classes, labels)
    sorted_labels = sorted(labels)
    aligned_sorted = aligned[:, [labels.index(l) for l in sorted_labels]]
    top3 = float(
        top_k_accuracy_score(y_true, aligned_sorted, k=min(3, len(labels)), labels=sorted_labels)
    )

    confidence = proba.max(axis=1) if proba.size else np.zeros(len(y_true))
    correct = (y_true == y_pred).astype(float)

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
        "macro_fpr": round(macro_fpr(np.asarray(y_true), np.asarray(y_pred), labels), 4),
        "top1_accuracy": round(top1, 4),
        "top3_accuracy": round(top3, 4),
        "ece": round(expected_calibration_error(confidence, correct), 4),
        "brier": round(multiclass_brier(np.asarray(y_true), aligned, labels), 4),
        "business": safe_auto_lane_coverage(np.asarray(y_true), confidence, np.asarray(y_pred)),
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
    calibration: str = "none"

    @property
    def name(self) -> str:
        return candidate_name(self.algo, self.calibration)


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


def select_candidate(
    algo_results: dict[str, AlgoResult],
    candidates: list[tuple[str, str]],
) -> str:
    """Pre-registered selection (SELECTION_RULE): validation safe auto-lane
    coverage (2dp ties) -> validation macro-F1 (3dp ties) -> validation ECE ->
    candidate order. ``None`` business components (a split with no
    auto-recoverable rows) sort last on the primary key."""
    order = {candidate_name(a, c): i for i, (a, c) in enumerate(candidates)}

    def key(name: str) -> tuple:
        m = algo_results[name].val_metrics
        safe = m["business"]["safe_auto_lane_coverage"]
        primary = round(safe, 2) if safe is not None else -2.0
        return (
            -primary,
            -round(m["macro_f1"], 3),
            m["ece"],
            order.get(name, len(order)),
        )

    return min(algo_results, key=key)


def train_and_compare(
    df: pd.DataFrame,
    *,
    seed: int = 42,
    train_frac: float = 0.6,
    val_frac: float = 0.2,
    calibrations: tuple[str, ...] = CALIBRATION_MODES,
    presplit: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame] | None = None,
) -> TrainingResult:
    """Fit every (algorithm x calibration) candidate, select on the
    pre-registered validation rule (SELECTION_RULE), report once on test.

    ``presplit`` supplies explicit (train, val, test) blocks instead of the
    default temporal split of ``df`` — used when the frame mixes sources that
    must each be split temporally (e.g. production detection windows +
    exact-span frames) so that a source's held-out block stays exactly the
    block earlier experiments reported against."""
    for mode in calibrations:
        if mode not in CALIBRATION_MODES:
            raise ValueError(f"unknown calibration {mode!r} (known: {CALIBRATION_MODES})")
    labels = validate_frame(df)
    if presplit is not None:
        train, val, test = presplit
        if not (len(train) and len(val) and len(test)):
            raise ValueError("presplit blocks must be non-empty")
        if len(train) + len(val) + len(test) != len(df):
            raise ValueError("presplit blocks must partition df")
    else:
        train, val, test = temporal_split(df, train_frac, val_frac)

    def xy(block: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        return (
            np.asarray([features_to_vector(r) for r in block[FEATURE_NAMES].to_dict("records")]),
            block["label"].to_numpy(),
        )

    X_train, y_train = xy(train)
    X_val, y_val = xy(val)
    X_test, y_test = xy(test)

    # Candidate order IS the final tie-break: LR > RF > GB, raw > sigmoid >
    # isotonic (baseline-first, simpler-first).
    candidates = [
        (algo, calibration) for algo in ALGO_ORDER for calibration in calibrations
    ]
    algo_results: dict[str, AlgoResult] = {}
    for algo, calibration in candidates:
        est = build_estimator(algo, seed, calibration)
        est.fit(X_train, y_train)
        val_metrics = compute_metrics(y_val, est.predict(X_val), est.predict_proba(X_val), est.classes_, labels)
        test_metrics = compute_metrics(y_test, est.predict(X_test), est.predict_proba(X_test), est.classes_, labels)
        result = AlgoResult(algo, est, val_metrics, test_metrics, calibration)
        algo_results[result.name] = result
        logger.info(
            "trained %s: val macro-F1=%.4f safe-lane=%s ece=%.4f | test macro-F1=%.4f top1=%.4f",
            result.name,
            val_metrics["macro_f1"],
            val_metrics["business"]["safe_auto_lane_coverage"],
            val_metrics["ece"],
            test_metrics["macro_f1"],
            test_metrics["top1_accuracy"],
        )

    best_algo = select_candidate(algo_results, candidates)
    best = algo_results[best_algo]

    version = make_model_version(df, seed)
    split_info = {
        "policy": (
            "per-source temporal blocks (presplit supplied by caller)"
            if presplit is not None
            else "temporal contiguous blocks sorted by window_end; no shuffling"
        ),
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
        "algo": result.best.algo,
        "calibration": result.best.calibration,
        "candidate": result.best_algo,
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
        "algo": result.best.algo,
        "calibration": result.best.calibration,
        "candidate": result.best_algo,
        "model_version": result.model_version,
        "trained_at": result.trained_at,
        "dataset": dataset_desc,
        "val_macro_f1": result.best.val_metrics["macro_f1"],
        "test_macro_f1": result.best.test_metrics["macro_f1"],
        "test_top1_accuracy": result.best.test_metrics["top1_accuracy"],
        "test_top3_accuracy": result.best.test_metrics["top3_accuracy"],
        "test_ece": result.best.test_metrics["ece"],
        "test_brier": result.best.test_metrics["brier"],
        "test_safe_auto_lane_coverage": result.best.test_metrics["business"][
            "safe_auto_lane_coverage"
        ],
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
        description="Diagnosis root-cause model comparison (LR/RF/GB x calibration, temporal split)",
        status="completed",
        started_at=datetime.fromisoformat(result.trained_at),
        ended_at=datetime.fromisoformat(result.trained_at),
        config={
            "algos": list(ALGO_ORDER),
            "calibrations": sorted({r.calibration for r in result.algo_results.values()}),
            "seed_dataset": dataset_desc,
            "feature_count": len(result.feature_names),
            "labels": result.labels,
            "split": result.split,
            "selection_rule": SELECTION_RULE,
        },
        results={
            "model_version": result.model_version,
            "best_algo": result.best_algo,
            "selection": SELECTION_RULE,
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
    "CALIBRATION_MODES",
    "CALIBRATION_CV_SPLITS",
    "AUTO_EXECUTE_THRESHOLD",
    "SELECTION_RULE",
    "AlgoResult",
    "TrainingResult",
    "ACTIVE_POINTER",
    "candidate_name",
    "build_estimator",
    "temporal_split",
    "expected_calibration_error",
    "multiclass_brier",
    "macro_fpr",
    "safe_auto_lane_coverage",
    "align_proba",
    "compute_metrics",
    "validate_frame",
    "select_candidate",
    "train_and_compare",
    "save_artifacts",
    "load_active_artifact",
    "persist_training_run",
    "persist_test_predictions",
]

"""Training pipeline: metrics structure, artifact roundtrip, and persistence
of the experiment + test-set predictions into the shared tables."""

import json

import numpy as np
import pytest

from app.models import Experiment, ModelPrediction
from app.services.diagnosis.taxonomy import CAUSES
from app.services.diagnosis.training import (
    ALGO_ORDER,
    ACTIVE_POINTER,
    load_active_artifact,
    persist_test_predictions,
    persist_training_run,
    validate_frame,
)


def test_all_algos_evaluated(tiny_trained):
    _, result = tiny_trained
    assert set(result.algo_results) == set(ALGO_ORDER)
    assert result.best_algo in ALGO_ORDER
    assert result.model_version.startswith("v")
    for algo, r in result.algo_results.items():
        for split in ("val_metrics", "test_metrics"):
            m = getattr(r, split)
            assert set(m["per_class"]) == set(CAUSES)
            for label, pm in m["per_class"].items():
                assert 0.0 <= pm["precision"] <= 1.0
                assert 0.0 <= pm["recall"] <= 1.0
                assert 0.0 <= pm["f1"] <= 1.0
                assert pm["support"] >= 0
            assert 0.0 <= m["top1_accuracy"] <= 1.0
            assert 0.0 <= m["top3_accuracy"] <= 1.0
            assert m["top3_accuracy"] >= m["top1_accuracy"] - 1e-9
            cm = m["confusion_matrix"]
            assert len(cm["matrix"]) == len(CAUSES)
            assert all(len(row) == len(CAUSES) for row in cm["matrix"])


def test_synthetic_separability_floor(tiny_trained):
    """The mini-generator's signatures are deliberately clean; the selected
    model should comfortably clear this floor. If it cannot, the feature
    pipeline (not the model) is almost certainly broken. Preliminary/synthetic
    numbers — see docs/ml.md."""
    _, result = tiny_trained
    assert result.best.test_metrics["top1_accuracy"] >= 0.6
    assert result.best.test_metrics["macro_f1"] >= 0.6


def test_artifact_roundtrip(tiny_trained):
    artifacts_dir, result = tiny_trained
    pointer = json.loads((artifacts_dir / ACTIVE_POINTER).read_text())
    assert pointer["model_version"] == result.model_version
    artifact = load_active_artifact(artifacts_dir)
    assert artifact is not None
    assert artifact["algo"] == result.best_algo
    assert artifact["feature_names"] == result.feature_names
    assert artifact["labels"] == result.labels
    vec = np.zeros((1, len(artifact["feature_names"])))
    proba = artifact["model"].predict_proba(vec)[0]
    assert proba.sum() == pytest.approx(1.0)


def test_load_active_artifact_missing(tmp_path):
    assert load_active_artifact(tmp_path) is None


def test_persist_training_run(db_session, tiny_trained):
    _, result = tiny_trained
    exp = persist_training_run(db_session, result, "synthetic-mini test")
    n = persist_test_predictions(db_session, result)
    db_session.commit()

    assert exp.id.startswith("exp_")
    assert exp.status == "completed"
    assert exp.results["model_version"] == result.model_version
    assert exp.results["best_algo"] == result.best_algo
    assert set(exp.results["algos"]) == set(ALGO_ORDER)
    assert exp.config["split"]["counts"]["test"] == n

    rows = db_session.query(ModelPrediction).filter_by(model_version=result.model_version).all()
    assert len(rows) == n
    sample = rows[0]
    assert sample.prediction_type == "diagnosis"
    assert sample.model_name == f"diagnosis-{result.best_algo}"
    assert set(sample.output) >= {"label", "true_label", "correct", "proba", "split", "heuristic"}
    assert sample.output["split"] == "test"
    assert sample.output["heuristic"] is False
    assert sample.output["label"] in CAUSES
    assert sample.score == pytest.approx(sample.output["proba"][sample.output["label"]])
    assert set(sample.input_features) == set(result.feature_names)


def test_validate_frame_rejects_bad_labels(tiny_trained):
    import pandas as pd

    _, result = tiny_trained
    row = {name: 0.0 for name in result.feature_names}
    bad = pd.DataFrame([{**row, "label": "not_a_cause", "window_end": pd.Timestamp.now(tz="UTC")}])
    with pytest.raises(ValueError, match="unknown labels"):
        validate_frame(bad)
    missing = pd.DataFrame([{"label": "no_fault", "window_end": pd.Timestamp.now(tz="UTC")}])
    with pytest.raises(ValueError, match="missing columns"):
        validate_frame(missing)

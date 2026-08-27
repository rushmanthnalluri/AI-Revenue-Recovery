"""Calibration + business-metric units: ECE, multiclass Brier, macro-FPR,
safe auto-lane coverage, calibrated estimator wiring (time-aware CV), and the
pre-registered candidate-selection rule."""

import numpy as np
import pytest
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit

from app.services.diagnosis.synthetic import SyntheticConfig, generate_dataset
from app.services.diagnosis.taxonomy import AUTO_RECOVERABLE_CAUSES, CAUSES
from app.services.diagnosis.training import (
    AUTO_EXECUTE_THRESHOLD,
    AlgoResult,
    build_estimator,
    compute_metrics,
    expected_calibration_error,
    macro_fpr,
    multiclass_brier,
    safe_auto_lane_coverage,
    select_candidate,
    train_and_compare,
)


class TestECE:
    def test_perfectly_calibrated_confident_correct_is_zero(self):
        # bin [0.9, 1.0]: mean confidence 0.95, accuracy 0.95 -> gap 0
        confidence = np.array([0.95, 0.95, 0.95, 0.95, 0.95,
                               0.95, 0.95, 0.95, 0.95, 0.95,
                               0.95, 0.95, 0.95, 0.95, 0.95,
                               0.95, 0.95, 0.95, 0.95, 0.95])
        correct = np.array([1.0] * 19 + [0.0])
        assert expected_calibration_error(confidence, correct) == pytest.approx(0.0)

    def test_fully_overconfident_is_one(self):
        confidence = np.array([1.0, 1.0, 1.0, 1.0])
        correct = np.array([0.0, 0.0, 0.0, 0.0])
        assert expected_calibration_error(confidence, correct) == pytest.approx(1.0)

    def test_known_mix(self):
        # bin [0.5,0.6): conf .55 correct 1 -> gap .45, weight 1/4
        # bin [0.9,1.0]: conf 1.0 x3, 1 correct -> gap 2/3, weight 3/4
        confidence = np.array([0.55, 1.0, 1.0, 1.0])
        correct = np.array([1.0, 1.0, 0.0, 0.0])
        expected = 0.25 * abs(1.0 - 0.55) + 0.75 * abs(1 / 3 - 1.0)
        assert expected_calibration_error(confidence, correct) == pytest.approx(expected)

    def test_empty_is_zero(self):
        assert expected_calibration_error(np.array([]), np.array([])) == 0.0


class TestBrier:
    def test_perfect_is_zero(self):
        labels = ["a", "b"]
        proba = np.array([[1.0, 0.0], [0.0, 1.0]])
        assert multiclass_brier(np.array(["a", "b"]), proba, labels) == pytest.approx(0.0)

    def test_worst_is_two(self):
        labels = ["a", "b"]
        proba = np.array([[0.0, 1.0]])  # certain and wrong
        assert multiclass_brier(np.array(["a"]), proba, labels) == pytest.approx(2.0)


class TestMacroFPR:
    def test_perfect_is_zero(self):
        y = np.array(["a", "b", "a", "b"])
        assert macro_fpr(y, y, ["a", "b"]) == pytest.approx(0.0)

    def test_all_wrong(self):
        y_true = np.array(["a", "a", "b", "b"])
        y_pred = np.array(["b", "b", "a", "a"])
        # per-class: FP/(FP+TN) = 2/(2+0) = 1 -> macro 1
        assert macro_fpr(y_true, y_pred, ["a", "b"]) == pytest.approx(1.0)


class TestSafeAutoLaneCoverage:
    def setup_method(self):
        self.auto = sorted(AUTO_RECOVERABLE_CAUSES)[0]  # an auto-recoverable class
        self.not_auto = next(c for c in CAUSES if c not in AUTO_RECOVERABLE_CAUSES)

    def test_only_auto_crosses_is_plus_one(self):
        y_true = np.array([self.auto, self.not_auto])
        confidence = np.array([0.9, 0.5])
        m = safe_auto_lane_coverage(y_true, confidence, np.array([self.auto, self.not_auto]))
        assert m["auto_coverage"] == pytest.approx(1.0)
        assert m["unsafe_coverage"] == pytest.approx(0.0)
        assert m["safe_auto_lane_coverage"] == pytest.approx(1.0)
        assert m["false_fire_rate"] == pytest.approx(0.0)

    def test_only_not_auto_crosses_is_minus_one(self):
        y_true = np.array([self.auto, self.not_auto])
        confidence = np.array([0.5, 0.9])
        m = safe_auto_lane_coverage(y_true, confidence, np.array([self.auto, self.not_auto]))
        assert m["safe_auto_lane_coverage"] == pytest.approx(-1.0)

    def test_nobody_crosses_is_zero(self):
        y_true = np.array([self.auto, self.not_auto])
        confidence = np.array([0.7, 0.7])  # heuristic-style capped confidence
        m = safe_auto_lane_coverage(y_true, confidence)
        assert m["safe_auto_lane_coverage"] == pytest.approx(0.0)

    def test_false_fire_requires_predicted_auto_class(self):
        # not-auto true, high confidence, predicted AUTO -> false fire counts
        y_true = np.array([self.not_auto, self.not_auto])
        confidence = np.array([0.95, 0.95])
        y_pred = np.array([self.auto, self.not_auto])
        m = safe_auto_lane_coverage(y_true, confidence, y_pred)
        assert m["unsafe_coverage"] == pytest.approx(1.0)  # both cross
        assert m["false_fire_rate"] == pytest.approx(0.5)  # one predicts auto

    def test_threshold_is_the_policy_floor(self):
        assert AUTO_EXECUTE_THRESHOLD == 0.85
        y_true = np.array([self.auto])
        m = safe_auto_lane_coverage(y_true, np.array([0.85]))
        assert m["auto_coverage"] == pytest.approx(1.0)  # >=, not >
        m = safe_auto_lane_coverage(y_true, np.array([0.8499]))
        assert m["auto_coverage"] == pytest.approx(0.0)


class TestCalibratedEstimator:
    def test_calibration_modes_wrap_in_calibrated_classifier_cv(self):
        assert not isinstance(build_estimator("random_forest", 1), CalibratedClassifierCV)
        for mode in ("sigmoid", "isotonic"):
            est = build_estimator("logistic_regression", 1, mode)
            assert isinstance(est, CalibratedClassifierCV)
            assert est.method == mode
            # time-aware CV: chronological folds inside the training block
            assert isinstance(est.cv, TimeSeriesSplit)

    def test_unknown_calibration_rejected(self):
        with pytest.raises(ValueError, match="unknown calibration"):
            build_estimator("random_forest", 1, "platt-scaled-maybe")

    def test_calibrated_proba_is_a_distribution(self, tiny_trained):
        # tiny_trained runs the full 9-candidate comparison; every candidate's
        # stored test probas must sum to 1 (checked via the service tests for
        # the winner; here: the raw estimator interface on one candidate).
        _, result = tiny_trained
        for row in result.test_proba:
            assert sum(row.values()) == pytest.approx(1.0, abs=1e-3)


def _fake_result(name: str, *, safe, f1, ece, order_cal="none") -> AlgoResult:
    algo = name.split("+")[0]
    val = {
        "macro_f1": f1,
        "ece": ece,
        "business": {"safe_auto_lane_coverage": safe},
    }
    return AlgoResult(algo, None, val, {}, order_cal)


class TestSelectCandidate:
    CANDIDATES = [
        (a, c)
        for a in ("logistic_regression", "random_forest", "gradient_boosting")
        for c in ("none", "sigmoid", "isotonic")
    ]

    def test_business_metric_wins_over_macro_f1(self):
        results = {
            "logistic_regression": _fake_result("logistic_regression", safe=0.5, f1=0.7, ece=0.01),
            "random_forest": _fake_result("random_forest", safe=0.4, f1=0.99, ece=0.01),
        }
        assert select_candidate(results, self.CANDIDATES) == "logistic_regression"

    def test_safe_tie_at_2dp_breaks_on_macro_f1(self):
        results = {
            "logistic_regression": _fake_result("logistic_regression", safe=0.501, f1=0.70, ece=0.01),
            "random_forest": _fake_result("random_forest", safe=0.502, f1=0.80, ece=0.01),
        }
        # both round to 0.50 -> macro-F1 decides
        assert select_candidate(results, self.CANDIDATES) == "random_forest"

    def test_f1_tie_at_3dp_breaks_on_ece(self):
        results = {
            "logistic_regression": _fake_result("logistic_regression", safe=0.5, f1=0.7001, ece=0.05),
            "random_forest": _fake_result("random_forest", safe=0.5, f1=0.7002, ece=0.01),
        }
        assert select_candidate(results, self.CANDIDATES) == "random_forest"

    def test_full_tie_breaks_to_candidate_order(self):
        results = {
            "random_forest": _fake_result("random_forest", safe=0.5, f1=0.7, ece=0.01),
            "logistic_regression": _fake_result("logistic_regression", safe=0.5, f1=0.7, ece=0.01),
        }
        assert select_candidate(results, self.CANDIDATES) == "logistic_regression"

    def test_none_safe_sorts_last(self):
        results = {
            "logistic_regression": _fake_result("logistic_regression", safe=None, f1=0.99, ece=0.0),
            "random_forest": _fake_result("random_forest", safe=-0.5, f1=0.5, ece=0.5),
        }
        assert select_candidate(results, self.CANDIDATES) == "random_forest"


def test_train_and_compare_extended_metric_keys():
    df = generate_dataset(SyntheticConfig(windows_per_class=10, seed=11))
    result = train_and_compare(df, seed=11, calibrations=("none", "sigmoid"))
    assert set(result.algo_results) == {
        "logistic_regression",
        "logistic_regression+sigmoid",
        "random_forest",
        "random_forest+sigmoid",
        "gradient_boosting",
        "gradient_boosting+sigmoid",
    }
    for r in result.algo_results.values():
        for m in (r.val_metrics, r.test_metrics):
            for key in ("macro_fpr", "ece", "brier", "business"):
                assert key in m
            assert m["business"]["threshold"] == 0.85
            assert m["business"]["auto_classes"] == sorted(AUTO_RECOVERABLE_CAUSES)

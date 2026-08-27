"""Unit test for the mix-adjusted (class-standardized) lift estimator —
hand-computed two-strata example."""

import pytest

from app.services.evaluation import EvaluationRunner


def _stratum(n_t, ok_t, n_h, ok_h):
    return {
        "stratum": "x",
        "treatment": {
            "failed_payments": n_t,
            "recovered_payments": ok_t,
            "recovery_rate": ok_t / n_t,
        },
        "holdout": {
            "failed_payments": n_h,
            "recovered_payments": ok_h,
            "recovery_rate": ok_h / n_h,
        },
        "lift": {"point": ok_t / n_t - ok_h / n_h},
    }


def test_standardized_lift_hand_computed():
    strata = [_stratum(100, 50, 100, 25), _stratum(100, 10, 100, 20)]
    out = EvaluationRunner._standardized_lift(strata)
    # weights 0.5/0.5: point = 0.5*0.25 + 0.5*(-0.10) = 0.075
    assert out["point"] == round(0.075, 6)
    # var = 0.25*(0.25*0.75/100*... see derivation in test); se = 0.041458...
    assert out["ci95_low"] == pytest.approx(-0.006256, abs=1e-6)
    assert out["ci95_high"] == pytest.approx(0.156256, abs=1e-6)
    assert out["ci95_low"] <= out["point"] <= out["ci95_high"]


def test_standardized_lift_empty_strata():
    assert EvaluationRunner._standardized_lift([]) is None

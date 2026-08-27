"""Evaluation harness package — see runner.py and docs/evaluation.md."""

from app.services.evaluation.runner import (
    CONVERSION,
    DETECTION_STEP_MINUTES,
    DETECTION_WINDOW_MINUTES,
    GATEWAY_SUCCESS_RATE,
    KIND_TO_CAUSE,
    OPERATOR,
    EvaluationRunner,
    truth_cause,
)

__all__ = [
    "CONVERSION",
    "DETECTION_STEP_MINUTES",
    "DETECTION_WINDOW_MINUTES",
    "GATEWAY_SUCCESS_RATE",
    "KIND_TO_CAUSE",
    "OPERATOR",
    "EvaluationRunner",
    "truth_cause",
]

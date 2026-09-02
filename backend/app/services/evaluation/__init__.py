"""Evaluation harness package — see runner.py and docs/evaluation.md."""

from app.services.evaluation.holdout import (
    CI_LEVEL,
    CI_Z,
    DEFAULT_HOLDOUT_FRACTION,
    HoldoutExcludingBuilder,
    holdout_token,
    is_holdout,
    median,
    newcombe_ci,
    wilson_interval,
)
from app.services.evaluation.outcomes import (
    ASSUMPTIONS,
    MIN_CELL,
    OutcomeModel,
    measure_outcomes,
)
from app.services.evaluation.runner import (
    DETECTION_STEP_MINUTES,
    DETECTION_WINDOW_MINUTES,
    KIND_TO_CAUSE,
    OPERATOR,
    EvaluationRunner,
    ScopedFailure,
    dataset_version,
    resolve_anchor,
    truth_cause,
)

__all__ = [
    "ASSUMPTIONS",
    "CI_LEVEL",
    "CI_Z",
    "DEFAULT_HOLDOUT_FRACTION",
    "DETECTION_STEP_MINUTES",
    "DETECTION_WINDOW_MINUTES",
    "HoldoutExcludingBuilder",
    "KIND_TO_CAUSE",
    "MIN_CELL",
    "OPERATOR",
    "EvaluationRunner",
    "OutcomeModel",
    "ScopedFailure",
    "dataset_version",
    "holdout_token",
    "is_holdout",
    "measure_outcomes",
    "median",
    "newcombe_ci",
    "resolve_anchor",
    "truth_cause",
    "wilson_interval",
]

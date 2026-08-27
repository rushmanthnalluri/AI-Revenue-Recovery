"""Diagnosis vertical: root-cause classification for payment incidents.

Public surface:
- ``DiagnosisService.classify(incident_id)`` — inference + persistence.
- ``compute_features`` / ``features_to_vector`` — the feature contract.
- ``generate_dataset`` / ``SyntheticConfig`` — standalone labeled data.
- ``train_and_compare`` / ``save_artifacts`` / ``temporal_split`` — training.
- ``heuristic_diagnose`` — rule-based cold-start fallback.
"""

from app.services.diagnosis.features import (
    FEATURE_NAMES,
    compute_features,
    compute_features_for_incident,
    features_to_vector,
    load_window_records,
)
from app.services.diagnosis.heuristic import heuristic_diagnose
from app.services.diagnosis.service import DiagnosisError, DiagnosisService
from app.services.diagnosis.synthetic import SyntheticConfig, generate_dataset
from app.services.diagnosis.taxonomy import CAUSES, CauseLabel
from app.services.diagnosis.training import (
    load_active_artifact,
    save_artifacts,
    temporal_split,
    train_and_compare,
)

__all__ = [
    "CAUSES",
    "CauseLabel",
    "FEATURE_NAMES",
    "compute_features",
    "compute_features_for_incident",
    "features_to_vector",
    "load_window_records",
    "heuristic_diagnose",
    "DiagnosisService",
    "DiagnosisError",
    "SyntheticConfig",
    "generate_dataset",
    "temporal_split",
    "train_and_compare",
    "save_artifacts",
    "load_active_artifact",
]

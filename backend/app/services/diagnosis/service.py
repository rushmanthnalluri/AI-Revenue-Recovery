"""DiagnosisService — incident root-cause classification.

``classify(incident_id)`` computes the feature vector for the incident window,
loads the active trained artifact (``backend/artifacts/diagnosis_active.json``
pointer), and persists:

- a ``diagnoses`` row (label, confidence, features JSON, human explanation,
  model name/version), and
- a ``model_predictions`` row carrying the structured output — full
  predict_proba distribution, top-3, and the ``heuristic`` flag — so every
  inference is auditable (architecture §7).

When no artifact exists, a deterministic rule-based fallback labels the
incident instead; the row is flagged ``heuristic=true`` in the prediction
output, uses model_name ``diagnosis-heuristic``, and its explanation starts
with ``[heuristic]``.

Re-scoping triage (rescope.py, default OFF — env ``DIAGNOSIS_WINDOW_RESCOPE``
or the constructor knob): before feature computation the detection window is
tightened to the floor-breaching span, so diluted scheduled-pass frames do
not wash out the signatures the model was selected on. Both frames are
recorded on the companion prediction row (``output["window"]``) and, when
the frame changed, on the explanation; the incident row is never mutated.

Schema note: the shared ``diagnoses`` table has no dedicated proba/top3/
heuristic columns and this package may not edit shared models — so the
structured output lives on the companion ``model_predictions`` row (the
table the architecture designates for "every model inference").
"""

import logging
from pathlib import Path
from typing import Any

import numpy as np
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Diagnosis, Incident, ModelPrediction
from app.services.diagnosis.features import (
    compute_features_for_incident,
    features_to_vector,
    incident_windows,
)
from app.services.diagnosis.heuristic import HEURISTIC_VERSION, heuristic_diagnose
from app.services.diagnosis.rescope import (
    RescopedWindow,
    rescope_enabled,
    rescope_incident_window,
)
from app.services.diagnosis.training import load_active_artifact

logger = logging.getLogger(__name__)

# backend/app/services/diagnosis/service.py -> parents[3] == backend/
DEFAULT_ARTIFACTS_DIR = Path(__file__).resolve().parents[3] / "artifacts"


class DiagnosisError(RuntimeError):
    """Raised when a diagnosis cannot be produced (unknown incident, bad window)."""


class DiagnosisService:
    def __init__(
        self,
        session: Session,
        artifacts_dir: Path | str | None = None,
        *,
        rescope_windows: bool | None = None,
    ) -> None:
        self.session = session
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else DEFAULT_ARTIFACTS_DIR
        # Window re-scoping triage knob: None defers to the
        # DIAGNOSIS_WINDOW_RESCOPE env var; default OFF keeps the published
        # evaluation anchors (measured on as-detected frames) deterministic.
        self.rescope_windows = rescope_windows

    # -- public API ---------------------------------------------------------

    def classify(self, incident_id: str) -> Diagnosis:
        incident = self.session.get(Incident, incident_id)
        if incident is None:
            raise DiagnosisError(f"incident {incident_id!r} not found")

        try:
            scope = self._resolve_scope(incident)
            window = (scope.scored_start, scope.scored_end) if scope.applied else None
            features = compute_features_for_incident(self.session, incident, window=window)
        except ValueError as exc:
            raise DiagnosisError(str(exc)) from exc

        artifact = load_active_artifact(self.artifacts_dir)
        if artifact is not None:
            outcome = self._predict_with_artifact(artifact, features)
        else:
            logger.info("no active diagnosis artifact at %s; using heuristic fallback", self.artifacts_dir)
            outcome = self._predict_with_heuristic(features)

        version = self._next_version(incident_id)
        explanation = self._explain(outcome, scope)
        diagnosis = Diagnosis(
            incident_id=incident_id,
            version=version,
            model_name=outcome["model_name"],
            model_version=outcome["model_version"],
            predicted_cause=outcome["label"],
            confidence=outcome["confidence"],
            features=features,
            explanation=explanation,
        )
        self.session.add(diagnosis)

        # Diagnosis fills the incident's root_cause (column exists for exactly
        # this purpose). Incident status transitions stay with the incident
        # lifecycle owner — not touched here.
        incident.root_cause = outcome["label"]

        self.session.add(
            ModelPrediction(
                incident_id=incident_id,
                model_name=outcome["model_name"],
                model_version=outcome["model_version"],
                prediction_type="diagnosis",
                input_features=features,
                output={
                    "label": outcome["label"],
                    "proba": outcome["proba"],
                    "top3": outcome["top3"],
                    "heuristic": outcome["heuristic"],
                    "reasons": outcome.get("reasons", []),
                    "window": scope.as_dict(),
                },
                score=outcome["confidence"],
            )
        )
        self.session.commit()
        logger.info(
            "diagnosed incident %s as %s (conf=%.3f, model=%s@%s, heuristic=%s, window_rescoped=%s)",
            incident_id,
            outcome["label"],
            outcome["confidence"],
            outcome["model_name"],
            outcome["model_version"],
            outcome["heuristic"],
            scope.applied,
        )
        return diagnosis

    # -- internals ----------------------------------------------------------

    def _resolve_scope(self, incident: Incident) -> RescopedWindow:
        """The frame diagnosis will score. When the knob is off — or the
        triage cannot prove a tighter span — this is the incident's own
        window. Triage failures degrade to the original frame (logged), never
        to a failed diagnosis."""
        if not rescope_enabled(self.rescope_windows):
            _, _, w_start, w_end = incident_windows(incident)
            return RescopedWindow(w_start, w_end, w_start, w_end, False, "disabled")
        try:
            return rescope_incident_window(self.session, incident)
        except ValueError:
            raise  # bad incident window — classify()'s error contract
        except Exception:  # noqa: BLE001 — triage must never break diagnosis
            logger.exception("window re-scoping failed for incident %s; scoring the original frame", incident.id)
            _, _, w_start, w_end = incident_windows(incident)
            return RescopedWindow(w_start, w_end, w_start, w_end, False, "error")

    def _next_version(self, incident_id: str) -> int:
        current = self.session.execute(
            sa.select(sa.func.max(Diagnosis.version)).where(Diagnosis.incident_id == incident_id)
        ).scalar()
        return int(current or 0) + 1

    @staticmethod
    def _predict_with_artifact(artifact: dict[str, Any], features: dict[str, float]) -> dict[str, Any]:
        model = artifact["model"]
        names = artifact["feature_names"]
        classes = [str(c) for c in artifact["labels"]]
        vector = np.asarray([features_to_vector(features, names)], dtype=float)
        proba = model.predict_proba(vector)[0]
        model_classes = [str(c) for c in model.classes_]
        # Align model output columns to the stored canonical label order.
        aligned = {c: 0.0 for c in classes}
        for j, cls in enumerate(model_classes):
            aligned[cls] = float(proba[j])
        order = sorted(classes, key=lambda c: aligned[c], reverse=True)
        top3 = [{"label": c, "probability": round(aligned[c], 6)} for c in order[:3]]
        return {
            "label": top3[0]["label"],
            "confidence": float(aligned[top3[0]["label"]]),
            "proba": {c: round(aligned[c], 6) for c in classes},
            "top3": top3,
            "heuristic": False,
            "model_name": f"diagnosis-{artifact['algo']}",
            "model_version": str(artifact["model_version"]),
        }

    @staticmethod
    def _predict_with_heuristic(features: dict[str, float]) -> dict[str, Any]:
        result = heuristic_diagnose(features)
        order = sorted(result["proba"], key=result["proba"].get, reverse=True)
        top3 = [{"label": c, "probability": round(result["proba"][c], 6)} for c in order[:3]]
        return {
            "label": result["label"],
            "confidence": result["confidence"],
            "proba": {c: round(p, 6) for c, p in result["proba"].items()},
            "top3": top3,
            "heuristic": True,
            "reasons": result["reasons"],
            "model_name": "diagnosis-heuristic",
            "model_version": HEURISTIC_VERSION,
        }

    @staticmethod
    def _explain(outcome: dict[str, Any], scope: RescopedWindow) -> str:
        top3 = ", ".join(f"{t['label']} {t['probability']:.2f}" for t in outcome["top3"])
        prefix = "[heuristic] " if outcome["heuristic"] else ""
        reasons = ""
        if outcome.get("reasons"):
            reasons = " Rules fired: " + "; ".join(outcome["reasons"]) + "."
        window_note = ""
        if scope.applied:
            window_note = (
                " Detection window re-scoped to the anomalous span "
                f"{scope.scored_start.isoformat()}..{scope.scored_end.isoformat()} "
                f"(detection frame {scope.original_start.isoformat()}..{scope.original_end.isoformat()})."
            )
        return (
            f"{prefix}Predicted {outcome['label']} "
            f"(confidence {outcome['confidence']:.2f}). Top-3: {top3}.{reasons}{window_note}"
        )


__all__ = ["DiagnosisService", "DiagnosisError", "DEFAULT_ARTIFACTS_DIR"]

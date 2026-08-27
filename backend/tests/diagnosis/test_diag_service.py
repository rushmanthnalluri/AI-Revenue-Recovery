"""DiagnosisService.classify(): end-to-end with a tiny trained artifact,
heuristic fallback when no artifact exists, and persistence guarantees."""

from datetime import timedelta

import pytest

from app.models import Diagnosis, ModelPrediction
from app.services.diagnosis.features import FEATURE_NAMES
from app.services.diagnosis.service import DiagnosisError, DiagnosisService
from app.services.diagnosis.taxonomy import CAUSES

from .conftest import T0


def _incident_for_window(make_incident):
    return make_incident(detected_at=T0 + timedelta(hours=1), window_start=T0, window_end=T0 + timedelta(hours=1))


def test_classify_end_to_end_bank_downtime(db_session, make_incident, make_window, tiny_trained):
    artifacts_dir, result = tiny_trained
    make_window("bank_downtime", seed=123)
    incident = _incident_for_window(make_incident)

    service = DiagnosisService(db_session, artifacts_dir=artifacts_dir)
    diag = service.classify(incident.id)

    # diagnoses row
    assert diag.id.startswith("dia_")
    assert diag.version == 1
    assert diag.predicted_cause == "bank_downtime"
    assert diag.model_name == f"diagnosis-{result.best_algo}"
    assert diag.model_version == result.model_version
    assert 0.0 < diag.confidence <= 1.0
    assert set(diag.features) == set(FEATURE_NAMES)
    assert diag.features["src_fail_share_w_bank"] > 0.5  # the window really has the signature
    assert diag.explanation and "bank_downtime" in diag.explanation
    db_session.refresh(incident)
    assert incident.root_cause == "bank_downtime"

    # companion model_predictions row: full proba distribution + top3 + flag
    pred = (
        db_session.query(ModelPrediction)
        .filter_by(incident_id=incident.id, prediction_type="diagnosis")
        .one()
    )
    assert pred.model_version == result.model_version
    assert pred.output["heuristic"] is False
    assert pred.output["label"] == "bank_downtime"
    assert set(pred.output["proba"]) == set(CAUSES)
    assert sum(pred.output["proba"].values()) == pytest.approx(1.0, abs=1e-4)
    assert len(pred.output["top3"]) == 3
    assert pred.output["top3"][0]["label"] == "bank_downtime"
    assert pred.score == pytest.approx(diag.confidence)

    # reclassification bumps the version
    diag2 = service.classify(incident.id)
    assert diag2.version == 2
    assert db_session.query(Diagnosis).filter_by(incident_id=incident.id).count() == 2


def test_classify_heuristic_fallback_without_artifact(db_session, make_incident, make_window, tmp_path):
    make_window("bank_downtime", seed=123)
    incident = _incident_for_window(make_incident)

    service = DiagnosisService(db_session, artifacts_dir=tmp_path)  # empty: no pointer
    diag = service.classify(incident.id)

    assert diag.model_name == "diagnosis-heuristic"
    assert diag.model_version == "heuristic-1"
    assert diag.predicted_cause == "bank_downtime"  # strong signature crosses the bank rule
    assert diag.confidence <= 0.7  # heuristic confidence is capped
    assert diag.explanation.startswith("[heuristic]")

    pred = db_session.query(ModelPrediction).filter_by(incident_id=incident.id).one()
    assert pred.output["heuristic"] is True
    assert pred.output["reasons"]  # which rules fired is recorded
    assert len(pred.output["top3"]) == 3


def test_classify_heuristic_no_events_is_no_fault(db_session, make_incident, tmp_path):
    incident = _incident_for_window(make_incident)  # nothing seeded
    service = DiagnosisService(db_session, artifacts_dir=tmp_path)
    diag = service.classify(incident.id)
    assert diag.predicted_cause == "no_fault"
    pred = db_session.query(ModelPrediction).filter_by(incident_id=incident.id).one()
    assert pred.output["heuristic"] is True


def test_classify_unknown_incident_raises(db_session, tmp_path):
    service = DiagnosisService(db_session, artifacts_dir=tmp_path)
    with pytest.raises(DiagnosisError, match="not found"):
        service.classify("inc_does_not_exist")

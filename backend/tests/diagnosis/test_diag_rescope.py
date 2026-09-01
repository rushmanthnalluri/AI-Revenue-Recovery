"""Window re-scoping triage (app.services.diagnosis.rescope): a diluted
scheduled-pass detection frame is tightened to the floor-breaching span
before diagnosis; already-tight frames pass through; the knob defaults OFF
so the published evaluation anchors keep their as-detected frames.

Data shape used below: a 12h detection window over quiet success traffic
with a failure burst in [+5h, +6h30) — on a 30-minute grid the breaching
buckets are exactly 5:00, 5:30 and 6:00, so the tight span is +5h..+6h30.
"""

from datetime import timedelta

import pytest

from app.models import ModelPrediction, Payment, PaymentEvent
from app.services.diagnosis.rescope import (
    ENV_RESCOPE,
    rescope_incident_window,
)
from app.services.diagnosis.service import DiagnosisService

from .conftest import T0

W_START = T0
W_END = T0 + timedelta(hours=12)
TIGHT_START = T0 + timedelta(hours=5)
TIGHT_END = T0 + timedelta(hours=6, minutes=30)

QUIET_COUNT = 120  # one captured every 300s over 12h, minus the 5h..7h band
BURST_COUNT = 60  # 3 buckets x (18 failed + 2 captured)


def _quiet_traffic() -> list[tuple[float, str]]:
    return [
        (float(ts), "captured")
        for ts in range(0, 12 * 3600, 300)
        if not (5 * 3600 <= ts < 7 * 3600)
    ]


def _burst(start_s: int, buckets: int = 3) -> list[tuple[float, str]]:
    events: list[tuple[float, str]] = []
    for b in range(buckets):
        base = start_s + b * 1800
        events += [(float(base + i * 90), "failed") for i in range(18)]
        events += [(float(base + 1600), "captured"), (float(base + 1700), "captured")]
    return events


def _baseline_traffic() -> list[tuple[float, str]]:
    return [(float(-3600 + i * 150), "captured") for i in range(20)]


def _seed_events(db_session, merchant, events, *, source_type="simulator") -> None:
    """Write (offset_seconds, outcome) rows relative to T0 as
    Payment+PaymentEvent pairs, tagged with the given provenance."""
    for offset, outcome in events:
        payment = Payment(
            merchant_id=merchant.id,
            amount_paise=10000,
            method="upi",
            status=outcome,
            source_type=source_type,
        )
        db_session.add(payment)
        db_session.flush()
        db_session.add(
            PaymentEvent(
                payment_id=payment.id,
                event_type=f"payment.{outcome}",
                to_status=outcome,
                source="simulator",
                occurred_at=T0 + timedelta(seconds=offset),
                payload={"bank": "hdfc"},
            )
        )
    db_session.commit()


@pytest.fixture()
def diluted(db_session, make_merchant):
    """Quiet 12h window with a +5h..+6h30 failure burst (simulator rows)."""
    merchant = make_merchant()
    _seed_events(db_session, merchant, _baseline_traffic() + _quiet_traffic() + _burst(5 * 3600))
    return merchant


@pytest.fixture()
def quiet(db_session, make_merchant):
    """The same window with NO burst — nothing breaches the floors."""
    merchant = make_merchant()
    _seed_events(db_session, merchant, _baseline_traffic() + _quiet_traffic())
    return merchant


def _diluted_incident(make_incident, **kw):
    meta = {
        "segment": {},
        "bucket_minutes": 30,
        "anomaly_start": TIGHT_START.isoformat(),
        "anomaly_end": TIGHT_END.isoformat(),
    }
    meta.update(kw.pop("meta", {}))
    return make_incident(
        detected_at=W_END,
        window_start=W_START,
        window_end=W_END,
        baseline_value=0.95,
        meta=meta,
        **kw,
    )


def _prediction(db_session, incident_id):
    return (
        db_session.query(ModelPrediction)
        .filter_by(incident_id=incident_id, prediction_type="diagnosis")
        .order_by(ModelPrediction.created_at.desc(), ModelPrediction.id.desc())
        .first()
    )


def test_diluted_window_rescoped_to_breach_span(db_session, make_incident, diluted, tmp_path):
    incident = _diluted_incident(make_incident)
    service = DiagnosisService(db_session, artifacts_dir=tmp_path, rescope_windows=True)
    service.classify(incident.id)

    window = _prediction(db_session, incident.id).output["window"]
    assert window["applied"] is True
    assert window["reason"] == "breach_span"
    assert window["scored_start"] == TIGHT_START.isoformat()
    assert window["scored_end"] == TIGHT_END.isoformat()
    # the incident's own frame is never mutated
    db_session.refresh(incident)
    assert incident.window_start == W_START
    assert incident.window_end == W_END


def test_diagnosis_scores_the_tight_frame(db_session, make_incident, diluted, tmp_path):
    incident = _diluted_incident(make_incident)
    service = DiagnosisService(db_session, artifacts_dir=tmp_path, rescope_windows=True)
    diag = service.classify(incident.id)

    # the tight frame holds the burst only: dilution is gone from the features
    assert diag.features["volume"] == float(BURST_COUNT)
    assert diag.features["failure_rate_w"] == pytest.approx(54 / 60, abs=1e-6)
    pred = _prediction(db_session, incident.id)
    assert pred.input_features["volume"] == float(BURST_COUNT)


def test_trained_artifact_also_receives_tight_frame(db_session, make_incident, diluted, tiny_trained):
    artifacts_dir, _ = tiny_trained
    incident = _diluted_incident(make_incident)
    service = DiagnosisService(db_session, artifacts_dir=artifacts_dir, rescope_windows=True)
    diag = service.classify(incident.id)

    pred = _prediction(db_session, incident.id)
    assert pred.output["heuristic"] is False  # the trained model ran...
    assert pred.input_features["volume"] == float(BURST_COUNT)  # ...on the tight frame
    assert pred.output["window"]["applied"] is True
    assert set(diag.features) == set(pred.input_features)


def test_already_tight_window_passes_through_unchanged(db_session, make_incident, diluted, tmp_path):
    incident = make_incident(
        detected_at=TIGHT_END,
        window_start=TIGHT_START,
        window_end=TIGHT_END,
        baseline_value=0.95,
        meta={"segment": {}, "bucket_minutes": 30},
    )
    service = DiagnosisService(db_session, artifacts_dir=tmp_path, rescope_windows=True)
    diag = service.classify(incident.id)

    window = _prediction(db_session, incident.id).output["window"]
    assert window["applied"] is False
    assert window["reason"] == "already_tight"
    assert window["scored_start"] == window["original_start"]
    assert window["scored_end"] == window["original_end"]
    assert diag.features["volume"] == float(BURST_COUNT)
    assert "re-scoped" not in (diag.explanation or "")


def test_knob_default_off_preserves_diluted_frame(db_session, make_incident, diluted, tmp_path):
    incident = _diluted_incident(make_incident)
    service = DiagnosisService(db_session, artifacts_dir=tmp_path)  # knob untouched
    diag = service.classify(incident.id)

    window = _prediction(db_session, incident.id).output["window"]
    assert window["applied"] is False
    assert window["reason"] == "disabled"
    # historical behavior: features over the full diluted 12h frame
    assert diag.features["volume"] == float(QUIET_COUNT + BURST_COUNT)
    assert "re-scoped" not in (diag.explanation or "")


def test_env_var_enables_rescoping(db_session, make_incident, diluted, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_RESCOPE, "1")
    incident = _diluted_incident(make_incident)
    service = DiagnosisService(db_session, artifacts_dir=tmp_path)  # knob defers to env
    service.classify(incident.id)

    window = _prediction(db_session, incident.id).output["window"]
    assert window["applied"] is True
    assert window["scored_start"] == TIGHT_START.isoformat()


def test_rescope_ignores_other_environment_rows(db_session, make_incident, make_merchant, diluted, tmp_path):
    # A second, LOUDER failure burst at +9h..+10h — but real_test provenance.
    # The research incident's series must not see it (environment boundary).
    other_merchant = make_merchant(name="Other Merchant")
    _seed_events(db_session, other_merchant, _burst(9 * 3600, buckets=2), source_type="razorpay_test")
    incident = _diluted_incident(make_incident)  # environment defaults to research
    service = DiagnosisService(db_session, artifacts_dir=tmp_path, rescope_windows=True)
    service.classify(incident.id)

    window = _prediction(db_session, incident.id).output["window"]
    assert window["applied"] is True
    assert window["scored_start"] == TIGHT_START.isoformat()
    assert window["scored_end"] == TIGHT_END.isoformat()  # not extended to +10h


def test_record_exposes_both_spans(db_session, make_incident, diluted, tmp_path):
    incident = _diluted_incident(make_incident)
    service = DiagnosisService(db_session, artifacts_dir=tmp_path, rescope_windows=True)
    diag = service.classify(incident.id)

    window = _prediction(db_session, incident.id).output["window"]
    assert set(window) == {
        "original_start",
        "original_end",
        "scored_start",
        "scored_end",
        "applied",
        "reason",
    }
    assert window["original_start"] == W_START.isoformat()
    assert window["original_end"] == W_END.isoformat()
    assert window["scored_start"] == TIGHT_START.isoformat()
    assert window["scored_end"] == TIGHT_END.isoformat()
    explanation = diag.explanation or ""
    assert "re-scoped" in explanation
    assert TIGHT_START.isoformat() in explanation
    assert W_START.isoformat() in explanation  # the detection frame stays on the record


# -- rescope_incident_window unit cases ------------------------------------


def test_unknown_metric_passes_through(db_session, make_incident, diluted):
    incident = _diluted_incident(make_incident, metric="webhook_delay_ms")
    scope = rescope_incident_window(db_session, incident)
    assert scope.applied is False
    assert scope.reason == "unknown_metric"
    assert (scope.scored_start, scope.scored_end) == (W_START, W_END)


def test_no_breach_falls_back_to_meta_anomaly_span(db_session, make_incident, quiet):
    incident = _diluted_incident(make_incident)  # meta carries the detector span
    scope = rescope_incident_window(db_session, incident)
    assert scope.applied is True
    assert scope.reason == "meta_anomaly_span"
    assert (scope.scored_start, scope.scored_end) == (TIGHT_START, TIGHT_END)


def test_no_breach_without_meta_passes_through(db_session, make_incident, quiet):
    incident = _diluted_incident(
        make_incident, meta={"anomaly_start": None, "anomaly_end": None}
    )
    scope = rescope_incident_window(db_session, incident)
    assert scope.applied is False
    assert scope.reason == "no_breach"

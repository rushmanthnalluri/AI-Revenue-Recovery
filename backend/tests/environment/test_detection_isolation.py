"""Detection environment isolation: a pass scores ONLY its own environment's
payment events, stamps incidents/evidence with that environment, and never
merges/dedups across the boundary. Uses the detection suite's deterministic
seeder (simulator-provenance rows by default)."""

import sqlalchemy as sa

import app.models as models
from app.schemas.detection import DetectionRunRequest
from app.services.detection import run_detection
from tests.detection.conftest import Stream


def _degrading_streams() -> list[Stream]:
    # 0.9 success for the first 36 buckets, collapse to 0.1 for the last 12 —
    # comfortably clears every incident-level floor for payment_success_rate.
    return [Stream(per_bucket=20, rate_at=lambda i: 0.9 if i < 36 else 0.1)]


def _request(environment: str) -> DetectionRunRequest:
    # The seed grid is 48 x 5-min buckets (4h); the window must cover the
    # healthy baseline AND the degraded tail.
    return DetectionRunRequest(environment=environment, window_minutes=240)


def _incidents(db_session) -> list[models.Incident]:
    return list(db_session.scalars(sa.select(models.Incident)))


def test_real_test_pass_over_simulator_data_detects_nothing(
    db_session, seed_payment_events
):
    seed_payment_events(streams=_degrading_streams())

    result = run_detection(db_session, _request("real_test"))
    assert result.anomalies_detected == 0
    assert result.incidents_created == []
    assert _incidents(db_session) == []


def test_research_pass_detects_and_stamps_research(db_session, seed_payment_events):
    seed_payment_events(streams=_degrading_streams())

    result = run_detection(db_session, _request("research"))
    assert result.incidents_created, result.detail
    incidents = _incidents(db_session)
    assert incidents, "research pass must persist incidents over simulator data"
    for inc in incidents:
        assert inc.environment == "research"
    evidence = list(db_session.scalars(sa.select(models.IncidentEvidence)))
    assert evidence, "incidents must carry evidence"
    for row in evidence:
        assert row.environment == "research"


def test_same_signature_detects_separately_per_environment(
    db_session, seed_payment_events
):
    """The identical degradation exists in both environments (the same rows
    re-tagged): each pass must create its OWN incident — dedup/merge/suppress
    never crosses the boundary."""
    seed_payment_events(streams=_degrading_streams())

    research = run_detection(db_session, _request("research"))
    assert research.incidents_created

    # Re-tag the very same commerce rows as Razorpay Test Mode data: a
    # real_test pass now sees them and must NOT merge into the research
    # incident (same metric/detector/segment fingerprint).
    db_session.execute(
        sa.update(models.Payment).values(
            source_type="razorpay_test", source_system="razorpay"
        )
    )
    db_session.commit()
    real = run_detection(db_session, _request("real_test"))
    assert real.incidents_created, real.detail
    assert set(real.incidents_created).isdisjoint(research.incidents_created)

    environments = {inc.environment for inc in _incidents(db_session)}
    assert environments == {"research", "real_test"}

    # And re-running each pass upserts within its own environment only.
    again = run_detection(db_session, _request("real_test"))
    assert again.incidents_created == []
    assert set(again.incidents_updated) <= set(real.incidents_created)

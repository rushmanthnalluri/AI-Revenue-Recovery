"""Read-only measured recovery outcome API and durable observation invariants."""

from app.db import utcnow
from app.models import RecoveryOutcomeObservation
from app.ports import ActionType, RecoveryStatus
from app.services.recovery.outcomes import record_outcome_observation


def test_outcome_rates_empty_real_environment_is_honest(client):
    response = client.get(
        "/api/v1/recovery/outcome-rates",
        params={"environment": "real_test", "days": 30},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["environment"] == "real_test"
    assert body["provenance"] == "measured_from_action_outcomes"
    assert body["cells"] == []
    assert body["organic"] == []
    assert body["incremental"] == []


def test_outcome_rates_reject_invalid_window(client):
    response = client.get(
        "/api/v1/recovery/outcome-rates",
        params={"environment": "research", "days": 181},
    )

    assert response.status_code == 422


def test_outcome_observation_is_idempotent_and_preserves_decision_time(
    db_session, make_opportunity, make_proposed_action
):
    opportunity = make_opportunity(environment="research")
    action = make_proposed_action(
        opportunity,
        action_type=ActionType.RETRY_PAYMENT,
        status=RecoveryStatus.UNKNOWN,
    )
    decision_at = action.decided_at or action.proposed_at
    first_observed_at = utcnow()

    first = record_outcome_observation(
        db_session,
        action,
        RecoveryStatus.UNKNOWN,
        source="test",
        observed_at=first_observed_at,
        evidence={"attempt": 1},
    )
    second = record_outcome_observation(
        db_session,
        action,
        RecoveryStatus.UNKNOWN,
        source="reconcile",
        observed_at=utcnow(),
        evidence={"attempt": 2},
    )
    db_session.commit()

    assert first is second
    assert second.decision_at == decision_at
    assert second.observed_at == first_observed_at
    assert second.source == "test"
    assert db_session.query(RecoveryOutcomeObservation).count() == 1

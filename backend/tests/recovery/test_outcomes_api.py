"""Read-only measured recovery outcome API: provenance and isolation."""


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

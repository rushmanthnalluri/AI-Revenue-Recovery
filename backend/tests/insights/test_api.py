"""API tests: GET /api/v1/incidents/{id} surfaces the additive insights object."""

from tests.insights.conftest import (
    BAD_REQUEST,
    BASELINE_START,
    TS_BASELINE,
    TS_WINDOW,
    WINDOW_END,
    WINDOW_START,
    seed_mix,
)

GATEWAY_ERROR = "GATEWAY_ERROR"

OUTLIER_KEYS = {
    "dimension",
    "value",
    "basis",
    "incident_rate",
    "baseline_rate",
    "lift",
    "support",
    "window_group_size",
    "baseline_group_size",
    "low_confidence",
}

CALLOUT_KEYS = {
    "dimension",
    "value",
    "classification",
    "platform_scope",
    "platform_window_rate",
    "platform_baseline_rate",
    "platform_lift",
    "platform_support",
    "summary",
}

COMPUTED_FROM_KEYS = {
    "window_start",
    "window_end",
    "baseline_start",
    "baseline_end",
    "segment",
    "window_payments",
    "window_failures",
    "baseline_payments",
    "baseline_failures",
}


def _seed(db_session, merchant, add_outcome):
    seed_mix(add_outcome, merchant, ts=TS_BASELINE, method="upi", bank="hdfc",
             n_success=45, failures=[(BAD_REQUEST, "insufficient_fund")] * 5)
    seed_mix(add_outcome, merchant, ts=TS_BASELINE, method="card", bank="hdfc",
             n_success=45, failures=[(BAD_REQUEST, "card_declined")] * 5)
    seed_mix(add_outcome, merchant, ts=TS_WINDOW, method="upi", bank="hdfc",
             n_success=25, failures=[(BAD_REQUEST, "insufficient_fund")] * 5)
    seed_mix(add_outcome, merchant, ts=TS_WINDOW, method="upi", bank="icici",
             n_success=0, failures=[(GATEWAY_ERROR, "bank_technical_error")] * 20)
    seed_mix(add_outcome, merchant, ts=TS_WINDOW, method="card", bank="hdfc",
             n_success=45, failures=[(BAD_REQUEST, "card_declined")] * 5)
    db_session.commit()


def _make_incident(make_incident, **kw):
    return make_incident(
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        detected_at=WINDOW_END,
        meta={"segment": {}},
        **kw,
    )


def test_detail_includes_insights_object(
    client, db_session, insights_merchant, add_outcome, make_incident
):
    _seed(db_session, insights_merchant, add_outcome)
    inc = _make_incident(make_incident)

    r = client.get(f"/api/v1/incidents/{inc.id}")
    assert r.status_code == 200, r.text
    insights = r.json()["insights"]
    assert insights is not None

    outliers = insights["outliers"]
    assert len(outliers) > 0
    assert all(set(o) == OUTLIER_KEYS for o in outliers)
    top = outliers[0]
    assert (top["dimension"], top["value"]) == ("bank", "icici")
    assert top["lift"] is None  # absent at baseline
    assert top["basis"] == "failure_rate"

    callout = insights["platform_callout"]
    assert callout is not None
    assert set(callout) == CALLOUT_KEYS
    assert callout["classification"] == "platform_wide"
    assert callout["platform_scope"] == "simulated_fleet"
    assert callout["summary"]

    cf = insights["computed_from"]
    assert set(cf) == COMPUTED_FROM_KEYS
    assert cf["window_start"].startswith(WINDOW_START.isoformat()[:19])
    assert cf["baseline_start"].startswith(BASELINE_START.isoformat()[:19])
    assert cf["segment"] == {}
    assert (cf["window_failures"], cf["baseline_failures"]) == (30, 10)

    # Deterministic over the wire: a second GET yields byte-identical insights.
    r2 = client.get(f"/api/v1/incidents/{inc.id}")
    assert r2.status_code == 200
    assert r2.json()["insights"] == insights


def test_detail_insights_empty_when_no_failures(
    client, db_session, insights_merchant, add_outcome, make_incident
):
    seed_mix(add_outcome, insights_merchant, ts=TS_WINDOW, method="upi",
             bank="hdfc", n_success=10, failures=[])
    db_session.commit()
    inc = _make_incident(make_incident)

    r = client.get(f"/api/v1/incidents/{inc.id}")
    assert r.status_code == 200, r.text
    insights = r.json()["insights"]
    assert insights["outliers"] == []
    assert insights["platform_callout"] is None
    assert insights["computed_from"]["window_failures"] == 0


def test_detail_insights_empty_when_no_payments_at_all(
    client, db_session, make_incident
):
    inc = _make_incident(make_incident)
    r = client.get(f"/api/v1/incidents/{inc.id}")
    assert r.status_code == 200, r.text
    insights = r.json()["insights"]
    assert insights["outliers"] == []
    assert insights["platform_callout"] is None
    assert insights["computed_from"]["window_payments"] == 0


def test_detail_insights_null_on_broken_window(client, db_session, make_incident):
    """A non-positive window must not take the whole detail page down."""
    inc = make_incident(
        window_start=WINDOW_END,
        window_end=WINDOW_END,
        detected_at=WINDOW_END,
        meta={"segment": {}},
    )
    r = client.get(f"/api/v1/incidents/{inc.id}")
    assert r.status_code == 200, r.text
    assert r.json()["insights"] is None


def test_detail_unknown_incident_still_404(client):
    r = client.get("/api/v1/incidents/inc_missing")
    assert r.status_code == 404

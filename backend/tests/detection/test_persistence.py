"""End-to-end detection runs via TestClient on in-memory SQLite: incident
persistence, evidence rows, idempotent re-runs, dry-run, segment localization,
and the latency metric."""

import sqlalchemy as sa

from app.models import Incident, IncidentEvidence
from app.ports import IncidentStatus
from tests.detection.conftest import Stream

RUN_BODY = {
    "window_minutes": 240,
    "bucket_minutes": 5,
    "detector": "zscore",
    "metrics": ["payment_success_rate"],
    "baseline_buckets": 12,
    "min_bucket_count": 5,
    # seeded fixtures are simulator-provenance: run in the research environment
    "environment": "research",
}


def _degraded(i: int) -> float:
    return 0.9 if i < 24 else 0.4  # -55.6% deviation from the 0.9 baseline


def test_run_creates_incident_with_evidence(client, db_session, seed_payment_events):
    seed_payment_events(streams=[Stream(rate_at=_degraded)])
    r = client.post("/api/v1/detection/run", json=RUN_BODY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["anomalies_detected"] == 1
    assert len(body["incidents_created"]) == 1

    view = body["incidents"][0]
    assert view["action"] == "created"
    assert view["metric"] == "payment_success_rate"
    assert view["detector"] == "zscore"
    assert view["severity"] == "CRITICAL"  # |deviation| >= 50%
    assert abs(view["baseline_value"] - 0.9) < 0.01
    assert abs(view["observed_value"] - 0.4) < 0.01
    assert view["deviation_pct"] <= -50
    assert view["affected_payments_count"] > 0
    assert view["revenue_at_risk_paise"] == view["affected_payments_count"] * 10000

    inc = db_session.get(Incident, view["incident_id"])
    assert inc is not None
    assert inc.status == IncidentStatus.OPEN
    assert inc.detection_method == "zscore"
    # detection latency is computable from what we persist:
    # detected_at (when we saw it) vs meta.anomaly_start (estimated start)
    assert inc.meta["anomaly_start"]
    assert inc.detected_at.tzinfo is not None
    assert inc.meta["bucket_minutes"] == 5

    evidence = db_session.scalars(
        sa.select(IncidentEvidence).where(IncidentEvidence.incident_id == inc.id)
    ).all()
    kinds = {e.evidence_type for e in evidence}
    assert kinds == {"metric_series", "segment_breakdown"}
    series_payload = next(e for e in evidence if e.evidence_type == "metric_series")
    assert len(series_payload.payload["buckets"]) >= 40
    assert all(e.collector == "agent:detection" for e in evidence)


def test_rerun_updates_instead_of_duplicating(client, db_session, seed_payment_events):
    seed_payment_events(streams=[Stream(rate_at=_degraded)])
    first = client.post("/api/v1/detection/run", json=RUN_BODY).json()
    inc_id = first["incidents_created"][0]
    detected_at = db_session.get(Incident, inc_id).detected_at

    second = client.post("/api/v1/detection/run", json=RUN_BODY).json()
    assert second["incidents_created"] == []
    assert second["incidents_updated"] == [inc_id]
    assert second["incidents"][0]["action"] == "updated"

    n_incidents = db_session.scalar(sa.select(sa.func.count()).select_from(Incident))
    assert n_incidents == 1
    inc = db_session.get(Incident, inc_id)
    assert inc.detected_at == detected_at  # original detection time preserved

    # evidence was refreshed, not stacked
    n_evidence = db_session.scalar(
        sa.select(sa.func.count())
        .select_from(IncidentEvidence)
        .where(IncidentEvidence.incident_id == inc_id)
    )
    assert n_evidence == 2


def test_dry_run_persists_nothing(client, db_session, seed_payment_events):
    seed_payment_events(streams=[Stream(rate_at=_degraded)])
    r = client.post("/api/v1/detection/run", json={**RUN_BODY, "dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["anomalies_detected"] == 1
    assert body["incidents"][0]["action"] == "would_create"
    assert body["incidents_created"] == []
    n = db_session.scalar(sa.select(sa.func.count()).select_from(Incident))
    assert n == 0


def test_segment_restricted_run(client, db_session, seed_payment_events):
    # card healthy throughout; upi collapses halfway (rates exact at 10/bucket)
    seed_payment_events(
        streams=[
            Stream(method="card", per_bucket=10, rate_at=lambda i: 0.9),
            Stream(
                method="upi",
                per_bucket=10,
                rate_at=lambda i: 0.9 if i < 24 else 0.30,
            ),
        ]
    )
    r = client.post(
        "/api/v1/detection/run",
        json={**RUN_BODY, "segment": {"method": "upi"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["anomalies_detected"] == 1
    view = body["incidents"][0]
    assert view["segment"] == {"method": "upi"}
    assert abs(view["baseline_value"] - 0.9) < 0.01
    assert abs(view["observed_value"] - 0.30) < 0.01

    inc = db_session.get(Incident, view["incident_id"])
    assert inc.meta["segment"] == {"method": "upi"}

    # a card-only run over the same window must stay quiet
    r2 = client.post(
        "/api/v1/detection/run",
        json={**RUN_BODY, "segment": {"method": "card"}},
    )
    assert r2.status_code == 200
    assert r2.json()["anomalies_detected"] == 0


def test_localization_flags_degraded_segment_globally(
    client, db_session, seed_payment_events
):
    # Whole-traffic run where only UPI degrades (card healthy throughout).
    seed_payment_events(
        streams=[
            Stream(method="card", bank="hdfc", per_bucket=10, rate_at=lambda i: 0.9),
            Stream(
                method="upi",
                bank="icici",
                per_bucket=10,
                rate_at=lambda i: 0.9 if i < 24 else 0.30,
            ),
        ]
    )
    r = client.post("/api/v1/detection/run", json=RUN_BODY)
    assert r.status_code == 200
    body = r.json()
    assert body["anomalies_detected"] == 1  # global rate dips 0.9 -> 0.6

    view = body["incidents"][0]
    inc = db_session.get(Incident, view["incident_id"])
    breakdown = next(e for e in inc.evidence if e.evidence_type == "segment_breakdown")
    dims = breakdown.payload["dimensions"]
    assert set(dims) == {"method", "bank", "gateway", "route"}

    # the fixtures carry no route tag: one "unknown" slice covering all
    # traffic (it mirrors the global drop, so its own flag follows the global
    # deviation — the segment-specific flags are asserted on method/bank)
    routes = {r["value"]: r for r in dims["route"]}
    assert set(routes) == {"unknown"}

    methods = {m["value"]: m for m in dims["method"]}
    assert set(methods) == {"card", "upi"}
    assert methods["upi"]["flagged"] is True
    assert methods["upi"]["deviation_pct"] < -50
    assert methods["card"]["flagged"] is False

    banks = {b["value"]: b for b in dims["bank"]}
    assert banks["icici"]["flagged"] is True
    assert banks["hdfc"]["flagged"] is False


def test_latency_metric_detection(client, db_session, seed_payment_events):
    seed_payment_events(
        streams=[
            Stream(
                rate_at=lambda i: 0.95,
                latency_at=lambda i: 250.0 if i < 24 else 1400.0,
            )
        ]
    )
    r = client.post(
        "/api/v1/detection/run",
        json={**RUN_BODY, "metrics": ["capture_latency_ms"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["anomalies_detected"] == 1
    view = body["incidents"][0]
    assert view["metric"] == "capture_latency_ms"
    assert view["deviation_pct"] > 100  # 250ms -> 1400ms
    assert abs(view["baseline_value"] - 250.0) < 1
    assert abs(view["observed_value"] - 1400.0) < 1


def test_detector_all_creates_one_incident_per_detector(
    client, db_session, seed_payment_events
):
    seed_payment_events(streams=[Stream(rate_at=_degraded)])
    r = client.post("/api/v1/detection/run", json={**RUN_BODY, "detector": "all"})
    assert r.status_code == 200
    body = r.json()
    methods = {i["detector"] for i in body["incidents"]}
    assert methods == {"zscore", "ewma", "cusum", "isolation_forest"}
    assert len(body["incidents_created"]) == 4


def test_unknown_detector_and_metric_return_400(client):
    r = client.post("/api/v1/detection/run", json={"detector": "nope"})
    assert r.status_code == 400
    assert "unknown_detector" in r.json()["error"]["code"]
    r2 = client.post("/api/v1/detection/run", json={"metrics": ["nope"]})
    assert r2.status_code == 400


def test_empty_database_completes_without_incidents(client):
    r = client.post("/api/v1/detection/run", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["anomalies_detected"] == 0
    assert body["incidents"] == []


def test_failed_then_captured_counts_as_success(client, db_session):
    """Razorpay semantics: payment.failed can be followed by payment.captured —
    the latest terminal event decides, so detection must not count it failed."""
    from datetime import timedelta

    from app.models import Merchant, Payment, PaymentEvent
    from app.services.detection.series import floor_bucket, load_outcomes
    from app.db import utcnow

    merchant = Merchant(name="Late-success Merchant")
    db_session.add(merchant)
    db_session.flush()
    ts = floor_bucket(utcnow(), 5) - timedelta(minutes=10)
    p = Payment(
        merchant_id=merchant.id,
        amount_paise=50000,
        status="captured",
        method="upi",
        gateway_created_at=ts,
    )
    db_session.add(p)
    db_session.flush()
    db_session.add_all(
        [
            PaymentEvent(
                payment_id=p.id,
                event_type="payment.failed",
                to_status="failed",
                source="seed",
                occurred_at=ts,
            ),
            PaymentEvent(
                payment_id=p.id,
                event_type="payment.captured",
                to_status="captured",
                source="seed",
                occurred_at=ts + timedelta(seconds=45),
            ),
        ]
    )
    db_session.commit()

    outcomes = load_outcomes(db_session, ts - timedelta(minutes=5), ts + timedelta(minutes=5))
    assert len(outcomes) == 1
    assert outcomes[0].success is True

"""Run-endpoint auditing (docs/security-testing.md residual recommendation).

POST /api/v1/detection/run and POST /api/v1/evaluation/run persist their own
run records; until now neither appended an audit_logs row. Every persisted
(non-dry-run) run now writes exactly one:
- detection: actor system:detection, action detection.run, stamped with the
  environment the pass scored (per-environment trail);
- evaluation: actor system:evaluation, action evaluation.run, research-stamped
  (both arms are simulator-derived scratch DBs — the system-scope convention,
  same as the demo reset's self-record).
"""

import sqlalchemy as sa

from app.models import AuditLog

API_KEY = {"X-API-Key": "dev-key"}
_TINY = {"days": 2, "events": 1200, "customers": 60}


def _run_audits(db_session, action: str) -> list[AuditLog]:
    return list(
        db_session.scalars(
            sa.select(AuditLog)
            .where(AuditLog.action == action)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )


class TestDetectionRunAuditing:
    def test_persisted_run_writes_an_audit_row(self, client, db_session):
        r = client.post(
            "/api/v1/detection/run",
            json={"environment": "research", "window_minutes": 60},
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        (row,) = _run_audits(db_session, "detection.run")
        assert row.entity_type == "detection_run"
        assert row.entity_id == run_id
        assert row.actor == "system:detection"
        assert row.environment == "research"  # the environment the pass scored
        assert row.details["environment"] == "research"
        assert row.details["baseline_mode"] == "leading_window"
        assert row.details["anomalies_detected"] == 0
        assert row.request_id  # the request-id middleware stamped it

    def test_default_environment_is_stamped_real_test(self, client, db_session):
        r = client.post("/api/v1/detection/run", json={})
        assert r.status_code == 200, r.text

        (row,) = _run_audits(db_session, "detection.run")
        assert row.environment == "real_test"

    def test_dry_run_writes_nothing(self, client, db_session):
        """A dry run persists nothing by contract — no incidents, and no
        audit row either (the audit row would itself be a persisted write)."""
        r = client.post(
            "/api/v1/detection/run",
            json={"environment": "research", "dry_run": True},
        )
        assert r.status_code == 200, r.text
        assert _run_audits(db_session, "detection.run") == []

    def test_unknown_detector_is_a_400_without_audit(self, client, db_session):
        r = client.post(
            "/api/v1/detection/run",
            json={"environment": "research", "detector": "not_a_detector"},
        )
        assert r.status_code == 400
        assert _run_audits(db_session, "detection.run") == []


class TestEvaluationRunAuditing:
    def test_completed_run_writes_an_audit_row(self, client, db_session):
        r = client.post(
            "/api/v1/evaluation/run",
            json={
                "name": "audit-eval",
                "scenario": "upi_outage_demo",
                "seed": 11,
                "holdout_fraction": 0.25,
                **_TINY,
            },
            headers=API_KEY,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "completed"

        (row,) = _run_audits(db_session, "evaluation.run")
        assert row.entity_type == "evaluation_run"
        assert row.entity_id == body["run_id"]
        assert row.actor == "system:evaluation"
        assert row.environment == "research"  # simulator-derived run record
        assert row.details["name"] == "audit-eval"
        assert row.details["scenario"] == "upi_outage_demo"
        assert row.details["seed"] == 11
        assert row.details["status"] == "completed"

    def test_validation_failure_writes_nothing(self, client, db_session):
        r = client.post(
            "/api/v1/evaluation/run",
            json={"scenario": "upi_outage_demo", "holdout_fraction": 2.0, **_TINY},
            headers=API_KEY,
        )
        assert r.status_code == 400
        assert _run_audits(db_session, "evaluation.run") == []

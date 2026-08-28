#!/usr/bin/env python
"""LIVE CHECK — classify one real seeded incident via TestClient.

Seeds a fresh simulator scenario (unseen seed+scale vs every training seed)
into an in-memory database, drives the REAL HTTP stack with FastAPI's
TestClient: scheduled-style detection passes via POST /api/v1/detection/run,
then GET /api/v1/incidents/{id} — which auto-diagnoses on first view through
DiagnosisService + the ACTIVE artifact in backend/artifacts/ (whatever
diagnosis_active.json points at when this script runs). Prints the persisted
diagnosis + confidence for the report.

Run from the repo root:

    backend/.venv/Scripts/python ml/experiments/diagnosis/live_check.py
"""

import dataclasses
import sys
from datetime import timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import sqlalchemy as sa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models  # noqa: E402,F401  (register tables)
from app.db import Base, get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Diagnosis, Incident, PaymentEvent  # noqa: E402
from app.services.detection.series import latest_event_anchor  # noqa: E402
from app.services.evaluation.runner import (  # noqa: E402
    DETECTION_STEP_MINUTES,
    DETECTION_WINDOW_MINUTES,
    load_ground_truth,
)
from app.simulator.config import SCENARIOS  # noqa: E402
from app.simulator.engine import run_simulation  # noqa: E402

SEED = 777  # unseen by every training dataset (v1: 5000-5071, v2: +5072-5143,
# v4: 6000-6143, pure-SR aug: 7000-7035)
DAYS, EVENTS = 5, 40_000  # standard preset; gateway_degradation is reliably
# detected at this density (the upi_outage_demo/8k variant's method_outage
# does not clear the volume floor — measured, that run is in the report)


def main() -> int:
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

    base = SCENARIOS["standard"][1]()
    config = dataclasses.replace(base, seed=SEED, days=DAYS, target_events=EVENTS)
    sim = run_simulation(config, session)
    gt = load_ground_truth(session, sim.run_id)
    print(f"seeded scenario standard seed={SEED} {DAYS}d/{EVENTS} events")
    for g in gt:
        print(f"  ground truth: {g.kind} cause={g.cause} [{g.start} .. {g.end})")

    app = create_app()

    def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app)

    # Scheduled-style passes: every 6h, 12h lookback — production schedule.
    anchor = latest_event_anchor(session)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    first = session.scalar(sa.select(sa.func.min(PaymentEvent.occurred_at)))
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    as_of = first + timedelta(minutes=DETECTION_WINDOW_MINUTES)
    passes = 0
    while as_of <= anchor + timedelta(minutes=DETECTION_STEP_MINUTES):
        resp = client.post(
            "/api/v1/detection/run",
            json={
                "as_of": min(as_of, anchor).isoformat(),
                "window_minutes": DETECTION_WINDOW_MINUTES,
            },
        )
        assert resp.status_code == 200, resp.text
        passes += 1
        as_of += timedelta(minutes=DETECTION_STEP_MINUTES)
    print(f"detection passes via TestClient: {passes}")

    incidents = session.scalars(sa.select(Incident)).all()
    print(f"incidents persisted: {len(incidents)}")
    for inc in incidents:
        # GET the incident — auto-diagnosis on first view (the live path).
        resp = client.get(f"/api/v1/incidents/{inc.id}")
        assert resp.status_code == 200, resp.text
        diag = session.scalar(
            sa.select(Diagnosis)
            .where(Diagnosis.incident_id == inc.id)
            .order_by(Diagnosis.version.desc())
            .limit(1)
        )
        span = (inc.meta or {}).get("anomaly_start"), (inc.meta or {}).get("anomaly_end")
        if diag is None:
            print(f"  incident {inc.id}: NO diagnosis (window={inc.window_start}..{inc.window_end})")
            continue
        print(f"  incident {inc.id} (metric={inc.metric}, anomaly={span})")
        print(f"    diagnosis: {diag.predicted_cause}  confidence={diag.confidence:.4f}")
        print(f"    model:     {diag.model_name} @ {diag.model_version}")
        print(f"    explanation: {diag.explanation}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

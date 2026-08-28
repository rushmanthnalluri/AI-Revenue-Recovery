#!/usr/bin/env python
"""exp08 hardening review — LIVE CHECK rerun on fresh unseen seeds/scales.

Same live path as exp07's live_check.py (seed 777): seed a fresh simulator
scenario into an in-memory DB, drive the REAL HTTP stack with FastAPI's
TestClient (scheduled-style POST /api/v1/detection/run passes, then
GET /api/v1/incidents/{id} which auto-diagnoses on first view through
DiagnosisService + the ACTIVE artifact). Adds what the eyeball pass did by
hand: every persisted incident is matched to ground truth with the SAME
labeling rule the training data used (prodframe-label-v1,
label_detection_window over the evidence span), so top-1 correctness is
mechanical, not judged.

Seeds/scales are CLI args so the two rerun configs live in config.json, not
in the script. Unseen-seed contract (training seeds): sim_features 1000-1059,
prod v1 5000-5071, v2 5072-5143, v4 6000-6143, aug 7000-7035; exp07 live
check 777. This review uses 888 and 1234.

Run from the repo root:

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp08_hardening_review/live_check_rerun.py \
        --seed 888 --days 4 --events 36000
"""

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import sqlalchemy as sa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: E402,F401  (register tables)
from app.db import Base, get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Diagnosis, Incident, PaymentEvent  # noqa: E402
from app.services.detection.series import latest_event_anchor  # noqa: E402
from app.services.diagnosis.prodframe import (  # noqa: E402
    GroundTruthSpan,
    evidence_span,
    label_detection_window,
)
from app.services.evaluation.runner import (  # noqa: E402
    DETECTION_STEP_MINUTES,
    DETECTION_WINDOW_MINUTES,
    load_ground_truth,
)
from app.simulator.config import SCENARIOS  # noqa: E402
from app.simulator.engine import run_simulation  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--days", type=int, required=True)
    ap.add_argument("--events", type=int, required=True)
    ap.add_argument("--tag", default="", help="output filename tag (default: seed<seed>)")
    args = ap.parse_args()
    tag = args.tag or f"seed{args.seed}"

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

    base = SCENARIOS["standard"][1]()
    config = dataclasses.replace(base, seed=args.seed, days=args.days, target_events=args.events)
    sim = run_simulation(config, session)
    gt = load_ground_truth(session, sim.run_id)
    gt_spans = [GroundTruthSpan(entity_id=g.entity_id, cause=g.cause, start=g.start, end=g.end) for g in gt]
    print(f"seeded scenario standard seed={args.seed} {args.days}d/{args.events} events")
    for g in gt:
        print(f"  ground truth: {g.kind} cause={g.cause} [{g.start} .. {g.end})")

    app = create_app()

    def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app)

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
    results = []
    for inc in incidents:
        resp = client.get(f"/api/v1/incidents/{inc.id}")
        assert resp.status_code == 200, resp.text
        diag = session.scalar(
            sa.select(Diagnosis)
            .where(Diagnosis.incident_id == inc.id)
            .order_by(Diagnosis.version.desc())
            .limit(1)
        )
        span_start, span_end = evidence_span(inc.meta, inc.window_start, inc.window_end)
        decision = label_detection_window(span_start, span_end, gt_spans)
        entry = {
            "incident_id": inc.id,
            "metric": inc.metric,
            "evidence_span": [span_start.isoformat(), span_end.isoformat()],
            "expected_label_prodframe_v1": decision.label,
            "expected_entity": decision.matched_entity_id,
            "overlap_seconds": decision.overlap_seconds,
        }
        if diag is None:
            entry.update({"diagnosis": None, "top1_correct": None})
            print(f"  incident {inc.id}: NO diagnosis (window={inc.window_start}..{inc.window_end})")
        else:
            entry.update(
                {
                    "diagnosis": diag.predicted_cause,
                    "confidence": round(float(diag.confidence), 4),
                    "model": f"{diag.model_name} @ {diag.model_version}",
                    "top1_correct": diag.predicted_cause == decision.label,
                    "explanation": diag.explanation,
                }
            )
            print(
                f"  incident {inc.id} (metric={inc.metric}, span={entry['evidence_span']}) "
                f"expected={decision.label}"
            )
            print(f"    diagnosis: {diag.predicted_cause}  confidence={diag.confidence:.4f}  "
                  f"top1_correct={entry['top1_correct']}")
            print(f"    model:     {diag.model_name} @ {diag.model_version}")
        results.append(entry)

    diagnosed = [r for r in results if r.get("diagnosis")]
    summary = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "days": args.days,
        "target_events": args.events,
        "scenario": "standard",
        "detection_passes": passes,
        "ground_truth_spans": [
            {"kind": g.kind, "cause": g.cause, "start": str(g.start), "end": str(g.end)} for g in gt
        ],
        "incidents_persisted": len(incidents),
        "incidents_diagnosed": len(diagnosed),
        "top1_correct": sum(1 for r in diagnosed if r["top1_correct"]),
        "top1_total": len(diagnosed),
        "model_versions_seen": sorted({r["model"] for r in diagnosed}),
        "results": results,
    }
    out = OUT_DIR / f"live_check_{tag}.json"
    out.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"top-1: {summary['top1_correct']}/{summary['top1_total']} | models: {summary['model_versions_seen']}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

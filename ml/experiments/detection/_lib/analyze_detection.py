"""Read-only detection analysis for the detection-recall track.

Replays the evaluation harness's *detection schedule* (and nothing else)
against a freshly simulated scratch dataset, then scores the persisted
incidents against ``simulator_ground_truth`` using the SAME matching rule as
``app.services.evaluation.runner`` (temporal overlap of the anomaly span with
the injected window). This yields the per-incident-kind recall the harness
itself does not report.

Nothing here touches the main database: the simulator seeds a throwaway
SQLite file in a temp directory, detection passes run there, and the file is
deleted afterwards (unless ``--keep-db`` is given).

Usage (from repo root or backend/):

    python ml/experiments/detection/_lib/analyze_detection.py \
        --scenario standard --seed 42 [--probe] [--json out.json]

Modes:
    default  replay the full 12h/6h schedule, score P/R/F1/MTTD overall and
             recall per ground-truth kind, list every persisted incident with
             its match (or FP) and every ground-truth incident with its
             first-match latency.
    --probe  additionally deep-dive each ground-truth incident: for every
             pass window overlapping it, whether the z-score detector fired
             per metric and which floor rejected it; plus raw bucket stats
             for the three measured blind spots (per-route latency, creations
             stuck in `created`, insufficient_fund share of failures).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[4] / "backend"
sys.path.insert(0, str(BACKEND))

import sqlalchemy as sa  # noqa: E402

from app.models import Incident, Payment, PaymentEvent  # noqa: E402
from app.schemas.detection import DetectionRunRequest  # noqa: E402
from app.services.detection import run_detection  # noqa: E402
from app.services.detection.detectors import DetectorParams, get_detector  # noqa: E402
from app.services.detection.engine import (  # noqa: E402
    DEFAULT_MIN_ABSOLUTE_DEVIATION,
    _flagged_run_and_volume,
)
from app.services.detection.series import (  # noqa: E402
    ATTEMPT_BASED_METRICS,
    KNOWN_METRICS,
    METRIC_CAPTURE_LATENCY,
    METRIC_DIRECTION,
    METRIC_SUCCESS_RATE,
    build_metric_series,
    build_series,
    floor_bucket,
    latest_event_anchor,
    load_checkout_attempts,
    load_outcomes,
)
from app.services.evaluation.runner import (  # noqa: E402
    DETECTION_STEP_MINUTES,
    DETECTION_WINDOW_MINUTES,
    _overlaps,
    load_ground_truth,
)
from app.simulator.cli import make_session  # noqa: E402
from app.simulator.config import SCENARIOS, SimulatorConfig  # noqa: E402
from app.simulator.engine import run_simulation  # noqa: E402

TERMINAL = ("captured", "authorized", "failed")
SUCCESS = ("captured", "authorized")


def _parse_ts(value: object) -> datetime:
    ts = datetime.fromisoformat(str(value))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# replay + scoring
# ---------------------------------------------------------------------------


def replay_schedule(db) -> int:
    """Identical pass schedule to EvaluationRunner._detect (production
    defaults; see runner.py). Returns the number of passes."""
    anchor = latest_event_anchor(db)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    window = timedelta(minutes=DETECTION_WINDOW_MINUTES)
    step = timedelta(minutes=DETECTION_STEP_MINUTES)
    first_event = db.scalar(sa.select(sa.func.min(PaymentEvent.occurred_at)))
    start = first_event or anchor
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    passes = 0
    as_of = start + window
    while as_of <= anchor + step:
        run_detection(
            db,
            DetectionRunRequest(
                as_of=min(as_of, anchor), window_minutes=DETECTION_WINDOW_MINUTES
            ),
        )
        passes += 1
        as_of += step
    return passes


def score(db, gt) -> dict:
    """Same scoring as EvaluationRunner._detect, plus per-kind recall and
    per-incident/per-GT detail."""
    incidents = list(db.scalars(sa.select(Incident).order_by(Incident.detected_at, Incident.id)))
    matched_gt: dict[str, dict] = {}
    per_incident = []
    true_positive = 0
    for inc in incidents:
        hit = next((g for g in gt if _overlaps(inc, g)), None)
        true_positive += hit is not None
        meta = inc.meta or {}
        per_incident.append(
            {
                "incident_id": inc.id,
                "metric": inc.metric,
                "detector": inc.detection_method,
                "segment": meta.get("segment", {}),
                "window": [inc.window_start.isoformat(), inc.window_end.isoformat()],
                "anomaly_span": [meta.get("anomaly_start"), meta.get("anomaly_end")],
                "deviation_pct": inc.deviation_pct,
                "severity": inc.severity.value if inc.severity else None,
                "affected_payments_count": inc.affected_payments_count,
                "revenue_at_risk_paise": inc.revenue_at_risk_paise,
                "matched_gt": hit.entity_id if hit else None,
                "matched_kind": hit.kind if hit else None,
            }
        )
        if hit is not None:
            seen_at = inc.window_end or inc.detected_at
            latency = max(0.0, (seen_at - hit.start).total_seconds() / 60)
            prev = matched_gt.get(hit.entity_id)
            if prev is None or latency < prev["mttd_minutes"]:
                matched_gt[hit.entity_id] = {
                    "entity_id": hit.entity_id,
                    "kind": hit.kind,
                    "start": hit.start.isoformat(),
                    "end": hit.end.isoformat(),
                    "mttd_minutes": round(latency, 2),
                    "first_matching_incident": inc.id,
                    "first_matching_metric": inc.metric,
                    "affected_amount_paise": hit.affected_amount_paise,
                    "recoverable": hit.recoverable,
                }
    precision = true_positive / len(incidents) if incidents else None
    recall = len(matched_gt) / len(gt) if gt else None
    f1 = (
        round(2 * precision * recall / (precision + recall), 6)
        if precision and recall and (precision + recall)
        else None
    )
    mttd_samples = [m["mttd_minutes"] for m in matched_gt.values()]
    per_kind = defaultdict(lambda: {"total": 0, "matched": 0})
    for g in gt:
        per_kind[g.kind]["total"] += 1
        per_kind[g.kind]["affected_amount_paise"] = g.affected_amount_paise
        per_kind[g.kind]["recoverable"] = g.recoverable
    for m in matched_gt.values():
        per_kind[m["kind"]]["matched"] += 1
    unmatched = [
        {
            "entity_id": g.entity_id,
            "kind": g.kind,
            "start": g.start.isoformat(),
            "end": g.end.isoformat(),
            "affected_amount_paise": g.affected_amount_paise,
            "recoverable": g.recoverable,
        }
        for g in gt
        if g.entity_id not in matched_gt
    ]
    return {
        "incidents": len(incidents),
        "matched_incidents": true_positive,
        "matched_ground_truth": len(matched_gt),
        "precision": round(precision, 6) if precision is not None else None,
        "recall": round(recall, 6) if recall is not None else None,
        "f1": f1,
        "mttd_minutes": (
            round(sum(mttd_samples) / len(mttd_samples), 2) if mttd_samples else None
        ),
        "per_kind_recall": {
            k: {
                "matched": v["matched"],
                "total": v["total"],
                "affected_amount_paise": v.get("affected_amount_paise", 0),
                "recoverable": v.get("recoverable"),
            }
            for k, v in sorted(per_kind.items())
        },
        "matched": sorted(matched_gt.values(), key=lambda m: m["start"]),
        "unmatched": unmatched,
        "per_incident": per_incident,
        "false_positives": [p for p in per_incident if p["matched_gt"] is None],
    }


# ---------------------------------------------------------------------------
# probes (failure analysis)
# ---------------------------------------------------------------------------


def _pass_windows(gt_start: datetime, gt_end: datetime, anchor: datetime):
    """Yield (as_of, window_start, window_end) for the scheduled passes whose
    window overlaps [gt_start, gt_end], plus one pass before/after."""
    step = timedelta(minutes=DETECTION_STEP_MINUTES)
    win = timedelta(minutes=DETECTION_WINDOW_MINUTES)
    as_of = floor_bucket(gt_start, 60) - win
    while as_of <= gt_end + win + step:
        ws, we = as_of - win, as_of
        if ws < gt_end and gt_start < we:
            yield as_of, ws, we
        as_of += step


def probe_detection_outcome(db, gt, detector_name: str = "zscore") -> list[dict]:
    """For each scheduled pass overlapping the GT window: did the detector
    fire per metric, and if it did, which floor rejected it?"""
    out = []
    detector = get_detector(detector_name)
    for as_of, ws, we in _pass_windows(gt.start, gt.end, gt.end):
        outcomes = load_outcomes(db, ws, we)
        row = {"as_of": as_of.isoformat(), "outcomes": len(outcomes)}
        for metric in KNOWN_METRICS:
            if metric in ATTEMPT_BASED_METRICS:
                records = load_checkout_attempts(db, ws, we)
            else:
                records = outcomes
            series = build_metric_series(
                records, metric=metric, window_start=ws, window_end=we,
                bucket_minutes=5,
            )
            params = DetectorParams(
                baseline_buckets=8, threshold=None, sensitivity=1.0,
                min_bucket_count=5, direction=METRIC_DIRECTION[metric],
                bucket_minutes=5,
            )
            anomaly = detector.detect(series, params)
            info: dict = {"fired": anomaly is not None}
            if anomaly is not None:
                longest, volume = _flagged_run_and_volume(anomaly, series)
                info.update(
                    deviation_pct=round(anomaly.deviation_pct, 2),
                    start=anomaly.start_ts.isoformat(),
                    n_flagged=len(anomaly.flagged_ts),
                    longest_run=longest,
                    volume=volume,
                    abs_dev=round(abs(anomaly.observed - anomaly.baseline), 4),
                    floor_default=DEFAULT_MIN_ABSOLUTE_DEVIATION.get(metric),
                )
            valid = [b for b in series if b.value is not None and b.count >= 5]
            info["valid_buckets"] = len(valid)
            row[metric] = info
        out.append(row)
    return out


def probe_route_latency(db, gt) -> dict:
    """Merchant-wide vs per-route mean capture latency per 5-min bucket
    around the route_latency window."""
    ws, we = gt.start - timedelta(hours=2), gt.end + timedelta(hours=1)
    outcomes = load_outcomes(db, ws, we)
    rows = []
    for o in outcomes:
        if o.latency_ms is None or not o.success:
            continue
        rows.append((floor_bucket(o.ts, 5), o.segments.get("route", "?"), o.latency_ms))
    by_bucket: dict = defaultdict(lambda: defaultdict(list))
    for ts, route, lat in rows:
        by_bucket[ts][route].append(lat)
    table = []
    for ts in sorted(by_bucket):
        entry = {"ts": ts.isoformat()}
        all_lat = [x for lats in by_bucket[ts].values() for x in lats]
        entry["all"] = {"n": len(all_lat), "mean_ms": round(sum(all_lat) / len(all_lat))}
        for route, lats in sorted(by_bucket[ts].items()):
            entry[route] = {"n": len(lats), "mean_ms": round(sum(lats) / len(lats))}
        table.append(entry)
    return {"window": [ws.isoformat(), we.isoformat()], "buckets": table}


def probe_abandonment(db, gt, inactivity_minutes: int = 30) -> dict:
    """Per 5-min bucket around the abandonment spike: payments created,
    resolved within the inactivity threshold, still created (censoring-aware
    vs the dataset anchor)."""
    anchor = latest_event_anchor(db)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    ws, we = gt.start - timedelta(hours=2), gt.end + timedelta(hours=1)
    payments = list(
        db.scalars(
            sa.select(Payment).where(
                Payment.created_at >= ws, Payment.created_at < we
            )
        )
    )
    terminal = dict(
        db.execute(
            sa.select(PaymentEvent.payment_id, sa.func.min(PaymentEvent.occurred_at))
            .where(PaymentEvent.to_status.in_(TERMINAL))
            .group_by(PaymentEvent.payment_id)
        ).all()
    )
    buckets: dict = defaultdict(lambda: {"created": 0, "resolved": 0, "stuck": 0, "censored": 0})
    thr = timedelta(minutes=inactivity_minutes)
    for p in payments:
        b = floor_bucket(p.created_at.replace(tzinfo=p.created_at.tzinfo or timezone.utc), 5)
        buckets[b]["created"] += 1
        t_term = terminal.get(p.id)
        horizon = p.created_at + thr
        if t_term is not None and t_term <= horizon:
            buckets[b]["resolved"] += 1
        elif horizon > anchor:
            buckets[b]["censored"] += 1
        else:
            buckets[b]["stuck"] += 1
    table = [
        {"ts": b.isoformat(), **v, "stuck_share_decidable": (
            round(v["stuck"] / (v["resolved"] + v["stuck"]), 4)
            if (v["resolved"] + v["stuck"]) else None
        )}
        for b, v in sorted(buckets.items())
    ]
    return {
        "window": [ws.isoformat(), we.isoformat()],
        "inactivity_minutes": inactivity_minutes,
        "anchor": anchor.isoformat(),
        "buckets": table,
    }


def probe_insufficient_fund_share(db, gt, bucket_minutes: int = 30) -> dict:
    """Per bucket around the insufficient-funds wave: failures total and
    insufficient_fund share (from the terminal event payload reason)."""
    ws, we = gt.start - timedelta(hours=3), min(gt.end + timedelta(hours=3),
                                                gt.start + timedelta(hours=27))
    rows = db.execute(
        sa.select(PaymentEvent)
        .where(
            PaymentEvent.occurred_at >= ws,
            PaymentEvent.occurred_at <= we,
            PaymentEvent.to_status == "failed",
        )
        .order_by(PaymentEvent.occurred_at.asc())
    ).scalars().all()
    # latest failed event per payment (a failure can be followed by capture;
    # only count payments whose LATEST terminal event is a failure)
    last: dict[str, PaymentEvent] = {}
    for ev in rows:
        last[ev.payment_id] = ev
    captured = set(
        db.scalars(
            sa.select(PaymentEvent.payment_id).where(
                PaymentEvent.occurred_at >= ws,
                PaymentEvent.occurred_at <= we,
                PaymentEvent.to_status.in_(SUCCESS),
            )
        ).all()
    )
    buckets: dict = defaultdict(lambda: {"failures": 0, "insufficient_fund": 0, "events": 0})
    for pid, ev in last.items():
        if pid in captured:
            continue  # late capture won — not a failure outcome
        b = floor_bucket(ev.occurred_at, bucket_minutes)
        buckets[b]["failures"] += 1
        reason = (ev.payload or {}).get("error_reason")
        if reason == "insufficient_fund":
            buckets[b]["insufficient_fund"] += 1
    ev_rows = db.execute(
        sa.select(PaymentEvent.occurred_at).where(
            PaymentEvent.occurred_at >= ws,
            PaymentEvent.occurred_at <= we,
            PaymentEvent.to_status.in_(TERMINAL),
        )
    ).all()
    for (ts,) in ev_rows:
        buckets[floor_bucket(ts, bucket_minutes)]["events"] += 1
    table = [
        {
            "ts": b.isoformat(),
            **v,
            "share": (round(v["insufficient_fund"] / v["failures"], 4) if v["failures"] else None),
        }
        for b, v in sorted(buckets.items())
    ]
    return {
        "window": [ws.isoformat(), we.isoformat()],
        "bucket_minutes": bucket_minutes,
        "buckets": table,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", default="standard")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--events", type=int, default=None)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--json", default=None, help="write the full report to this path")
    p.add_argument("--keep-db", default=None, help="keep the scratch DB at this path")
    args = p.parse_args()

    factory = SCENARIOS[args.scenario][1]
    base: SimulatorConfig = factory()
    overrides = {
        k: v
        for k, v in {
            "seed": args.seed, "days": args.days, "target_events": args.events
        }.items()
        if v is not None
    }
    config = dataclasses.replace(base, **overrides)

    tmpdir = Path(tempfile.mkdtemp(prefix="pulserecover_detanalysis_"))
    url = f"sqlite:///{tmpdir / 'analysis.db'}"
    session = make_session(url)
    report: dict = {"config": config.config_dict(), "simulator_run_id": None}
    try:
        sim = run_simulation(config, session)
        report["simulator_run_id"] = sim.run_id
        gt = load_ground_truth(session, sim.run_id)
        report["ground_truth"] = [
            {
                "entity_id": g.entity_id, "kind": g.kind,
                "start": g.start.isoformat(), "end": g.end.isoformat(),
                "recoverable": g.recoverable,
                "affected_amount_paise": g.affected_amount_paise,
            }
            for g in gt
        ]
        report["passes"] = replay_schedule(session)
        report["detection"] = score(session, gt)

        if args.probe:
            probes = {}
            for g in gt:
                entry: dict = {
                    "kind": g.kind,
                    "window": [g.start.isoformat(), g.end.isoformat()],
                    "detection_outcome": probe_detection_outcome(session, g),
                }
                if g.kind == "route_latency":
                    entry["route_latency"] = probe_route_latency(session, g)
                elif g.kind == "checkout_abandonment_spike":
                    entry["abandonment"] = probe_abandonment(session, g)
                elif g.kind == "customer_insufficient_funds_wave":
                    entry["insufficient_fund_share"] = probe_insufficient_fund_share(session, g)
                probes[g.entity_id] = entry
            report["probes"] = probes
    finally:
        if args.keep_db:
            shutil.copy(tmpdir / "analysis.db", args.keep_db)
        bind = session.get_bind()
        session.close()
        bind.dispose()
        shutil.rmtree(tmpdir, ignore_errors=True)

    d = report["detection"]
    print(f"simulator_run_id={report['simulator_run_id']} passes={report['passes']}")
    print(
        f"incidents={d['incidents']} matched_rows={d['matched_incidents']} "
        f"matched_gt={d['matched_ground_truth']}/{len(report['ground_truth'])}"
    )
    print(
        f"precision={d['precision']} recall={d['recall']} f1={d['f1']} "
        f"mttd_min={d['mttd_minutes']}"
    )
    print("per-kind recall:")
    for kind, v in d["per_kind_recall"].items():
        print(
            f"  {kind}: {v['matched']}/{v['total']} "
            f"(affected INR {v['affected_amount_paise'] / 100:,.0f}, recoverable={v['recoverable']})"
        )
    print("false positives:")
    for fp in d["false_positives"]:
        print(
            f"  {fp['incident_id']} {fp['metric']} dev={fp['deviation_pct']}% "
            f"span={fp['anomaly_span']}"
        )
    print("unmatched ground truth:")
    for u in d["unmatched"]:
        print(
            f"  {u['kind']} {u['start']}..{u['end']} "
            f"INR {u['affected_amount_paise'] / 100:,.0f}"
        )

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

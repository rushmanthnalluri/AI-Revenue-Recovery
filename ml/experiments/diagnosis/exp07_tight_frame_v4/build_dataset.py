#!/usr/bin/env python
"""exp07 — build the v4 frame families: prod_frames_v3 + tight_frames_v1.

Why this exists (the exp06 SHIP_VERDICT follow-up, three measured gaps):

1. **Tight ad-hoc frames are missing from every training set.** v2/v3 contain
   only 12h scheduled windows (+ exact spans); production ALSO serves tight
   180/240-min ad-hoc windows (the demo's detection runs, API
   investigations). The v2 model hedged a slam-dunk 180-min gateway
   degradation to 0.239 (measured, exp06/DECISION_log.md). This build emits
   one row per persisted incident from DEMO-SHAPE ad-hoc passes
   (metrics=["payment_success_rate"], bucket_minutes=10, baseline_buckets=6,
   anchored gt_end+25min, window 180 AND 240) plus one API-investigation
   shape (all metrics, default 5-min buckets, 180-min) per ground-truth
   incident, plus one quiet-anchor probe per seed.
2. **Density cycle extended to 70k events/day.** Demo A runs 140k events in
   2 days (70k/day); both v2 and v3 top out at 30k/day — a measured OOD
   driver of the demo-A hedging (exp06/SHIP_VERDICT.md). The scale cycle
   adds (2d, 140k).
3. **Rebuilt on the CURRENT stabilized detection engine.** prod_frames_v2's
   shard2 was built while the detection track's new metrics were landing
   (exp04/CONTAMINATION.md; ~14% of rows may include
   checkout_abandonment_rate / insufficient_fund_share incidents from
   in-flight code). v3 uses fresh seeds 6000-6143 against the stabilized
   engine (all four KNOWN_METRICS live; KNOWN_METRICS list recorded in
   config.json), so v3 is a single-code-version dataset.

Design decisions (recorded, deliberate):

- **One scratch DB per seed.** The scheduled stream runs FIRST with full
  production behavior (dedup on); tight passes run AFTER with
  ``dedup_cooldown_minutes=None`` so multiple frame variants of the same
  episode can coexist as rows. Each tight row is exactly the incident
  production would persist if that ad-hoc pass were the first to see the
  episode (the demo's situation: one pass on a fresh DB). Detection
  series/floors read payment_events only — the scheduled stream's incidents
  cannot influence a tight pass's detection, only its dedup (disabled).
  The exact same-window upsert stays active, so the all-metrics pass
  refreshes rather than duplicates the demo-shape row for the same
  (metric, window) — those two rows would be feature-identical anyway
  (features are window-based, not bucket-based).
- **Same jitter + stagger conventions as exp01** (imported, not copied):
  severity/placement jitter, 37h stagger, same labeling rule
  (``prodframe-label-v1``), same feature code path as inference
  (``compute_features_for_incident``).
- **Quiet probes:** one 180-min all-metrics pass per seed at a seeded anchor
  >=6h clear of every ground-truth span (skipped when no such anchor fits a
  short sim). Persisted rows (rare, post-floors) are honest tight-frame
  ``no_fault`` examples; passes that persist nothing emit no row, matching
  production (no incident -> no diagnosis).

Run from the repo root (Windows Git Bash), ~1-2 min/seed:

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp07_tight_frame_v4/build_dataset.py
"""

import dataclasses
import json
import random
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
EXP01_DIR = REPO_ROOT / "ml" / "experiments" / "diagnosis" / "exp01_prod_frames_dataset"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(EXP01_DIR))

import sqlalchemy as sa  # noqa: E402

from app.models import Incident, PaymentEvent  # noqa: E402
from app.schemas.detection import DetectionRunRequest  # noqa: E402
from app.services.detection.engine import run_detection  # noqa: E402
from app.services.detection.series import KNOWN_METRICS, latest_event_anchor  # noqa: E402
from app.services.diagnosis.features import FEATURE_NAMES, compute_features_for_incident  # noqa: E402
from app.services.diagnosis.prodframe import (  # noqa: E402
    LABELING_RULE_VERSION,
    GroundTruthSpan,
    evidence_span,
    label_detection_window,
)
from app.services.evaluation.runner import (  # noqa: E402  (harness composition root)
    DETECTION_STEP_MINUTES,
    DETECTION_WINDOW_MINUTES,
    _ScratchDb,
    load_ground_truth,
)
from app.simulator.config import SCENARIOS  # noqa: E402
from app.simulator.engine import run_simulation  # noqa: E402

# exp01 machinery, reused unchanged (jitter namespace, git sha helper):
from build_dataset import _git_sha, _jitter_incidents  # noqa: E402

# ---------------------------------------------------------------------------
# Experiment configuration (recorded verbatim in config.json)
# ---------------------------------------------------------------------------

SEED_START = 6000  # fresh range: sim_features 1000-1059, prod v1/v2 5000-5143
N_SEEDS = 144
CUSTOMERS = 1_500
OUT_PROD_CSV = BACKEND / "artifacts" / "prod_frames_v3.csv"
OUT_TIGHT_CSV = BACKEND / "artifacts" / "tight_frames_v1.csv"

_SCENARIO_CYCLE = ("standard", "storm", "upi_outage_demo", "payday_wave_demo")

# Density cycle = exp01's six points + (2d, 140k) = demo A's 70k events/day
# (the measured OOD driver of the demo-A hedging, exp06/SHIP_VERDICT.md).
_SCALE_CYCLE = (
    (15, 30_000),
    (10, 20_000),
    (2, 60_000),
    (5, 40_000),
    (2, 30_000),
    (7, 25_000),
    (2, 140_000),
)

TIGHT_WINDOWS = (180, 240)  # demo B/C/D/E and demo A detection windows
TIGHT_ANCHOR_LAG = timedelta(minutes=25)  # the demo's anchor: inc_end + 25min
QUIET_CLEARANCE = timedelta(hours=6)


def _run_scheduled_stream(db, stats: dict) -> None:
    """The production schedule, unmodified (exp01's loop): a pass every STEP
    over the simulated span, each looking back WINDOW, request defaults
    (all KNOWN_METRICS, production floors, dedup on)."""
    anchor = latest_event_anchor(db)
    if anchor is None:
        stats["detail"] = "no terminal events"
        return
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    window = timedelta(minutes=DETECTION_WINDOW_MINUTES)
    step = timedelta(minutes=DETECTION_STEP_MINUTES)
    first = db.scalar(sa.select(sa.func.min(PaymentEvent.occurred_at)))
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    as_of = first + window
    while as_of <= anchor + step:
        result = run_detection(
            db,
            DetectionRunRequest(
                as_of=min(as_of, anchor), window_minutes=DETECTION_WINDOW_MINUTES
            ),
        )
        stats["scheduled_passes"] += 1
        stats["anomalies_filtered"] += result.anomalies_filtered
        as_of += step


def _run_tight_passes(db, gt, seed: int, anchor, stats: dict) -> None:
    """Ad-hoc tight frames, dedup disabled (see module docstring): per
    ground-truth incident, the demo's request shape at 180 AND 240 min plus
    the API-investigation shape (all metrics, default buckets) at 180 min,
    each anchored gt_end+25min like the demo; plus one quiet-anchor probe."""
    for g in gt:
        as_of = min(g.end + TIGHT_ANCHOR_LAG, anchor)
        for window_minutes in TIGHT_WINDOWS:
            run_detection(
                db,
                DetectionRunRequest(
                    metrics=["payment_success_rate"],
                    window_minutes=window_minutes,
                    bucket_minutes=10,
                    baseline_buckets=6,
                    as_of=as_of,
                    dedup_cooldown_minutes=None,
                ),
            )
            stats["tight_passes"] += 1
        run_detection(
            db,
            DetectionRunRequest(
                window_minutes=180,
                as_of=as_of,
                dedup_cooldown_minutes=None,
            ),
        )
        stats["tight_passes"] += 1

    # Quiet probe: a seeded anchor well clear of every ground-truth span.
    rng = random.Random(f"tightframe-quiet:{seed}")
    first = db.scalar(sa.select(sa.func.min(PaymentEvent.occurred_at)))
    if first is not None:
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        lo, hi = first + timedelta(minutes=200), anchor - timedelta(minutes=10)
        for _ in range(20):
            if (hi - lo).total_seconds() <= 0:
                break
            probe = lo + timedelta(seconds=rng.uniform(0, (hi - lo).total_seconds()))
            if all(
                probe - QUIET_CLEARANCE >= g.end or probe + QUIET_CLEARANCE <= g.start
                for g in gt
            ):
                run_detection(
                    db,
                    DetectionRunRequest(
                        window_minutes=180, as_of=probe, dedup_cooldown_minutes=None
                    ),
                )
                stats["tight_passes"] += 1
                stats["quiet_probe"] = probe.isoformat(timespec="seconds")
                break


def _row(db, inc, decision, gt_spans, family: str) -> dict:
    features = compute_features_for_incident(db, inc)
    window_minutes = int((inc.window_end - inc.window_start).total_seconds() // 60)
    return {
        **{name: features[name] for name in FEATURE_NAMES},
        "label": decision.label,
        "window_start": inc.window_start.isoformat(timespec="seconds"),
        "window_end": inc.window_end.isoformat(timespec="seconds"),
        # metadata (not training features — audit/analysis only)
        "frame_family": family,
        "frame_window_minutes": window_minutes,
        "metric": inc.metric,
        "detector": inc.detection_method,
        "deviation_pct": inc.deviation_pct,
        "matched_entity_id": decision.matched_entity_id or "",
        "overlap_seconds": decision.overlap_seconds,
        "overlapping_entity_ids": "|".join(decision.overlapping_entity_ids),
        "n_gt_spans": len(gt_spans),
    }


def collect_seed_rows(seed: int, idx: int, base_end: datetime) -> tuple[list[dict], list[dict], dict]:
    """One seeded scenario -> (scheduled-family rows, tight-family rows, stats)."""
    scenario = _SCENARIO_CYCLE[seed % len(_SCENARIO_CYCLE)]
    days, events = _SCALE_CYCLE[idx % len(_SCALE_CYCLE)]
    base = SCENARIOS[scenario][1]()
    config = dataclasses.replace(
        base, seed=seed, days=days, target_events=events, customers=CUSTOMERS
    )
    # 37h stagger (exp01/export convention): without it the temporal split
    # bands whole classes into single blocks (measured, docs/ml.md §8).
    config = dataclasses.replace(config, end_date=base_end - timedelta(hours=37 * idx))
    config = _jitter_incidents(config, seed)

    stats = {
        "seed": seed,
        "scenario": scenario,
        "days": days,
        "target_events": events,
        "scheduled_passes": 0,
        "tight_passes": 0,
        "anomalies_filtered": 0,
        "incidents_scheduled": 0,
        "incidents_tight": 0,
    }
    prod_rows: list[dict] = []
    tight_rows: list[dict] = []
    with _ScratchDb() as scratch:
        db = scratch.session
        sim = run_simulation(config, db)
        gt = load_ground_truth(db, sim.run_id)
        gt_spans = [
            GroundTruthSpan(entity_id=g.entity_id, cause=g.cause, start=g.start, end=g.end)
            for g in gt
        ]
        gt_aware = [
            dataclasses.replace(
                g,
                start=g.start.replace(tzinfo=timezone.utc) if g.start.tzinfo is None else g.start,
                end=g.end.replace(tzinfo=timezone.utc) if g.end.tzinfo is None else g.end,
            )
            for g in gt
        ]

        # 1) scheduled stream FIRST (production behavior, dedup on)
        _run_scheduled_stream(db, stats)

        # 2) tight ad-hoc passes AFTER (dedup disabled per module docstring)
        anchor = latest_event_anchor(db)
        if anchor is None:
            stats["detail"] = "no terminal events"
            return prod_rows, tight_rows, stats
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        _run_tight_passes(db, gt_aware, seed, anchor, stats)

        # 3) one row per persisted incident; family told apart by window length
        for inc in db.scalars(sa.select(Incident)):
            span_start, span_end = evidence_span(inc.meta, inc.window_start, inc.window_end)
            decision = label_detection_window(span_start, span_end, gt_spans)
            family = (
                "scheduled"
                if inc.window_end - inc.window_start == timedelta(minutes=DETECTION_WINDOW_MINUTES)
                else "tight"
            )
            row = _row(db, inc, decision, gt_spans, family)
            row["seed"] = seed
            row["scenario"] = scenario
            row["incident_id"] = inc.id
            (prod_rows if family == "scheduled" else tight_rows).append(row)
        stats["incidents_scheduled"] = len(prod_rows)
        stats["incidents_tight"] = len(tight_rows)
    return prod_rows, tight_rows, stats


FIELDNAMES = [
    *FEATURE_NAMES,
    "label",
    "window_start",
    "window_end",
    "frame_family",
    "frame_window_minutes",
    "metric",
    "detector",
    "deviation_pct",
    "matched_entity_id",
    "overlap_seconds",
    "overlapping_entity_ids",
    "n_gt_spans",
    "seed",
    "scenario",
    "incident_id",
]


def _write_csv(path: Path, rows: list[dict]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _summary(rows: list[dict]) -> dict:
    label_dist = Counter(r["label"] for r in rows)
    return {
        "rows": len(rows),
        "label_distribution": dict(sorted(label_dist.items())),
        "no_fault_share": round(label_dist.get("no_fault", 0) / max(len(rows), 1), 4),
        "per_scenario_rows": dict(sorted(Counter(r["scenario"] for r in rows).items())),
        "per_window_minutes": dict(
            sorted(Counter(str(r["frame_window_minutes"]) for r in rows).items())
        ),
        "per_metric": dict(sorted(Counter(r["metric"] for r in rows).items())),
        "multi_overlap_rows": sum(1 for r in rows if "|" in r["overlapping_entity_ids"]),
    }


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed-start", type=int, default=SEED_START)
    p.add_argument("--n-seeds", type=int, default=N_SEEDS)
    args = p.parse_args()

    exp_dir = Path(__file__).resolve().parent
    base_end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    started = datetime.now(timezone.utc)

    prod_rows: list[dict] = []
    tight_rows: list[dict] = []
    seed_stats: list[dict] = []
    seeds = [args.seed_start + i for i in range(args.n_seeds)]
    for idx, seed in enumerate(seeds):
        p_rows, t_rows, stats = collect_seed_rows(seed, idx, base_end)
        prod_rows.extend(p_rows)
        tight_rows.extend(t_rows)
        seed_stats.append(stats)
        print(
            f"[{idx + 1:>3}/{len(seeds)}] seed={seed} {stats['scenario']:<18} "
            f"{stats['days']}d/{stats['target_events']} ev -> "
            f"scheduled {stats['incidents_scheduled']}, tight {stats['incidents_tight']}",
            flush=True,
        )

    _write_csv(OUT_PROD_CSV, prod_rows)
    _write_csv(OUT_TIGHT_CSV, tight_rows)

    summary = {
        "labeling_rule_version": LABELING_RULE_VERSION,
        "prod_frames_v3": _summary(prod_rows),
        "tight_frames_v1": _summary(tight_rows),
        "seed_stats": seed_stats,
    }
    config = {
        "experiment": "exp07_tight_frame_v4",
        "script": str(Path(__file__).relative_to(REPO_ROOT)),
        "argv": sys.argv,
        "labeling_rule_version": LABELING_RULE_VERSION,
        "git_sha": _git_sha(),
        "known_metrics_at_build": list(KNOWN_METRICS),
        "started_utc": started.isoformat(),
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "base_end_anchor_utc": base_end.isoformat(),
        "seeds": {"start": args.seed_start, "count": args.n_seeds, "list": seeds},
        "scenario_cycle": list(_SCENARIO_CYCLE),
        "scale_cycle_days_events": [list(s) for s in _SCALE_CYCLE],
        "customers": CUSTOMERS,
        "scheduled_stream": {
            "step_minutes": DETECTION_STEP_MINUTES,
            "window_minutes": DETECTION_WINDOW_MINUTES,
            "request": "DetectionRunRequest defaults (production floors, dedup on)",
        },
        "tight_passes": {
            "windows_minutes": list(TIGHT_WINDOWS),
            "anchor": "ground-truth end + 25min (the demo's anchor), clamped to latest event",
            "demo_shape": "metrics=['payment_success_rate'], bucket_minutes=10, baseline_buckets=6",
            "adhoc_shape": "all KNOWN_METRICS, default buckets (5/8), 180-min only",
            "dedup_cooldown_minutes": None,
            "dedup_note": "disabled so multiple frame variants of one episode coexist; "
            "each row is what production persists if that pass runs on fresh state",
            "quiet_probe": "one 180-min all-metrics pass/seed, seeded anchor >=6h clear of every gt span",
        },
        "jitter": "exp01 build_dataset._jitter_incidents, namespace prodframe-jitter:{seed} (unchanged)",
        "outputs": {
            "prod_frames_v3": "backend/artifacts/prod_frames_v3.csv",
            "tight_frames_v1": "backend/artifacts/tight_frames_v1.csv",
        },
    }
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (exp_dir / "dataset_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nwrote {len(prod_rows)} scheduled rows to {OUT_PROD_CSV}")
    print(f"wrote {len(tight_rows)} tight rows to {OUT_TIGHT_CSV}")
    print("prod labels:", summary["prod_frames_v3"]["label_distribution"])
    print("tight labels:", summary["tight_frames_v1"]["label_distribution"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

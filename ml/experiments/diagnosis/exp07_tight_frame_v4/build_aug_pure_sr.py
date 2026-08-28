#!/usr/bin/env python
"""exp07 iteration 2 — targeted augmentation: pure success-rate-drop frames.

The measured hole (run_v4.py iteration 1, metrics.json + the feature check on
the real demo-A frame): demo scenario A is a DOCUMENTED pure success-rate
drop — ``latency_multiplier=1.0`` by design (scripts/demo_run.py config_a
docstring: "Latency is left unmultiplied so the signature is a pure
success-rate drop"). On its real frame the latency deltas are ~0
(p50_delta -17ms, p90_delta_ratio -0.03; demo D, a latency-2.5x degradation,
shows +3095ms / +0.83). But EVERY gateway_degradation row in v4 comes from
the standard/storm presets (latency 2.5x/3.0x, jitter floored at 1.75x) —
the latency~=1.0 degradation signature is absent from training at any
density. Measured cost: best prod-gate candidate (random_forest) reads the
demo-A frame at 0.635 gateway_degradation confidence (needs >= 0.867);
logistic_regression reads 0.864 (needs 0.867).

This build adds ONLY that missing signature: single-incident pure-SR-drop
gateway degradations (latency_multiplier pinned to 1.0, fail_boost jittered
over the mild-to-moderate band that includes demo A's 0.12), at BOTH demo
densities (70k events/day = demo A, 30k/day = demo D), on fresh seeds
7000-7035, with the same frame collection as the main v4 build (scheduled
12h passes + demo-shape tight 180/240 + all-metric 180, dedup rules
identical, same prodframe-label-v1 labeling, same feature code path).

Scope discipline: one iteration, one measured hypothesis. The ship gate in
run_v4.py is UNCHANGED; if no candidate passes every clause on v4b, the
outcome is a documented NO-SHIP with both iterations on record.

Run from the repo root:

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp07_tight_frame_v4/build_aug_pure_sr.py
"""

import dataclasses
import json
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
EXP01_DIR = REPO_ROOT / "ml" / "experiments" / "diagnosis" / "exp01_prod_frames_dataset"
sys.path.insert(0, str(BACKEND))

import importlib.util as _ilu  # noqa: E402

# Load the exp07 main build under a UNIQUE module name: its own top-level
# `from build_dataset import ...` must keep resolving to exp01's module, which
# breaks if this file is imported under the literal name "build_dataset".
_spec = _ilu.spec_from_file_location(
    "exp07_build_dataset", Path(__file__).resolve().parent / "build_dataset.py"
)
_exp07build = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_exp07build)

import sqlalchemy as sa  # noqa: E402

from app.models import Incident  # noqa: E402
from app.services.diagnosis.prodframe import (  # noqa: E402
    LABELING_RULE_VERSION,
    GroundTruthSpan,
    evidence_span,
    label_detection_window,
)
from app.services.evaluation.runner import (  # noqa: E402
    DETECTION_WINDOW_MINUTES,
    _ScratchDb,
    load_ground_truth,
)
from app.services.detection.series import latest_event_anchor  # noqa: E402
from app.simulator.config import IncidentKind, IncidentSpec, SimulatorConfig  # noqa: E402
from app.simulator.engine import run_simulation  # noqa: E402

# exp07 main-build machinery, reused verbatim (same frames, dedup rules, row shape):
FIELDNAMES = _exp07build.FIELDNAMES
_git_sha = _exp07build._git_sha
_row = _exp07build._row
_run_scheduled_stream = _exp07build._run_scheduled_stream
_run_tight_passes = _exp07build._run_tight_passes
_write_csv = _exp07build._write_csv

SEED_START = 7000  # fresh: sim_features 1000-1059, v2 5000-5143, v4 6000-6143
N_SEEDS = 36
# Density: demo A is 140k/2d (70k/day) — the documented OOD driver; demo D is
# 60k/2d (30k/day). Two-thirds of seeds at demo-A density (that is where the
# miss binds), one-third at demo-D density for coverage.
_DENSITY = [(2, 140_000)] * 24 + [(2, 60_000)] * 12
OUT_CSV = BACKEND / "artifacts" / "aug_pure_sr_v1.csv"


def _pure_sr_config(seed: int, idx: int, base_end: datetime) -> SimulatorConfig:
    """Single pure-SR-drop gateway degradation, placement/severity jittered."""
    rng = random.Random(f"pure-sr-aug:{seed}")
    days, events = _DENSITY[idx % len(_DENSITY)]
    spec = IncidentSpec(
        IncidentKind.GATEWAY_DEGRADATION,
        day_fraction=1.0,  # last simulated day (the demo's placement)
        start_hour_ist=rng.uniform(4.0, 20.0),
        duration_hours=round(rng.uniform(2.0, 3.0), 2),
        params={
            "fail_boost": round(rng.uniform(0.08, 0.35), 4),
            "latency_multiplier": 1.0,  # THE signature: pure success-rate drop
        },
    )
    return SimulatorConfig(
        seed=seed,
        days=days,
        target_events=events,
        customers=1_500,
        scenario="pure_sr_drop_aug",
        end_date=base_end - timedelta(hours=37 * idx),
        incidents=(spec,),
    )


def collect_seed_rows(seed: int, idx: int, base_end: datetime):
    config = _pure_sr_config(seed, idx, base_end)
    stats = {"seed": seed, "days": config.days, "target_events": config.target_events,
             "fail_boost": config.incidents[0].params["fail_boost"],
             "duration_hours": config.incidents[0].duration_hours,
             "scheduled_passes": 0, "tight_passes": 0, "anomalies_filtered": 0,
             "incidents_scheduled": 0, "incidents_tight": 0}
    rows: list[dict] = []
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
        _run_scheduled_stream(db, stats)
        anchor = latest_event_anchor(db)
        if anchor is None:
            stats["detail"] = "no terminal events"
            return rows, stats
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        _run_tight_passes(db, gt_aware, seed, anchor, stats)
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
            row["scenario"] = "pure_sr_drop_aug"
            row["incident_id"] = inc.id
            rows.append(row)
        stats["incidents_scheduled"] = sum(1 for r in rows if r["frame_family"] == "scheduled")
        stats["incidents_tight"] = sum(1 for r in rows if r["frame_family"] == "tight")
    return rows, stats


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed-start", type=int, default=SEED_START)
    p.add_argument("--n-seeds", type=int, default=N_SEEDS)
    args = p.parse_args()

    exp_dir = Path(__file__).resolve().parent
    base_end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    started = datetime.now(timezone.utc)

    rows: list[dict] = []
    seed_stats: list[dict] = []
    seeds = [args.seed_start + i for i in range(args.n_seeds)]
    for idx, seed in enumerate(seeds):
        r, stats = collect_seed_rows(seed, idx, base_end)
        rows.extend(r)
        seed_stats.append(stats)
        print(
            f"[{idx + 1:>2}/{len(seeds)}] seed={seed} {stats['days']}d/{stats['target_events']} ev "
            f"boost={stats['fail_boost']} -> scheduled {stats['incidents_scheduled']}, "
            f"tight {stats['incidents_tight']}",
            flush=True,
        )

    _write_csv(OUT_CSV, rows)
    label_dist = Counter(r["label"] for r in rows)
    summary = {
        "labeling_rule_version": LABELING_RULE_VERSION,
        "rows": len(rows),
        "label_distribution": dict(sorted(label_dist.items())),
        "per_family": dict(sorted(Counter(r["frame_family"] for r in rows).items())),
        "per_density_events": dict(
            sorted(Counter(s["target_events"] for s in seed_stats).items())
        ),
        "seed_stats": seed_stats,
    }
    config = {
        "experiment": "exp07_tight_frame_v4 iteration 2 (pure-SR-drop augmentation)",
        "script": str(Path(__file__).relative_to(REPO_ROOT)),
        "argv": sys.argv,
        "hypothesis": "latency_multiplier=1.0 gateway degradations are absent from v4 "
        "(jitter floor 1.75x) but are exactly demo A's documented signature; "
        "demo-A frame latency deltas measured ~0 (p50 -17ms, p90 ratio -0.03)",
        "labeling_rule_version": LABELING_RULE_VERSION,
        "git_sha": _git_sha(),
        "started_utc": started.isoformat(),
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "base_end_anchor_utc": base_end.isoformat(),
        "seeds": {"start": args.seed_start, "count": args.n_seeds, "list": seeds},
        "density_cycle_days_events": {"(2, 140000)": 24, "(2, 60000)": 12},
        "incident": "single GATEWAY_DEGRADATION, latency_multiplier=1.0 pinned, "
        "fail_boost U(0.08, 0.35), duration U(2.0, 3.0)h, start_hour_ist U(4, 20), "
        "day_fraction 1.0, 37h stagger",
        "frame_collection": "identical to build_dataset.py (scheduled + tight, same dedup rules)",
        "output_csv": "backend/artifacts/aug_pure_sr_v1.csv",
    }
    (exp_dir / "aug_pure_sr_config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (exp_dir / "aug_pure_sr_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {len(rows)} rows to {OUT_CSV}")
    print("labels:", dict(sorted(label_dist.items())))
    print("families:", summary["per_family"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

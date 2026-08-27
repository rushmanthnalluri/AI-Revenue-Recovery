#!/usr/bin/env python
"""Build the production-frame diagnosis dataset (experiment exp01).

One row per PERSISTED DETECTION INCIDENT, collected by running the real
detection engine (``app.services.detection.engine.run_detection`` — imported,
never modified) on scheduled passes (every 6h, 12h lookback — the production
schedule from the evaluation harness) over multi-seed simulator output. Each
row is labeled from ``simulator_ground_truth`` by the documented rule in
``app/services/diagnosis/prodframe.py`` (LABELING_RULE_VERSION). Features are
computed with the SAME code path inference uses
(``compute_features_for_incident`` on the incident's persisted window).

Run from the repo root (Windows Git Bash):

    backend/.venv/Scripts/python ml/experiments/diagnosis/exp01_prod_frames_dataset/build_dataset.py

Deterministic per config (same code version): simulator draws are seeded;
the only wall-clock input is the base end-date anchor (recorded in
config.json), same convention as app.services.evaluation.export_training.
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
sys.path.insert(0, str(BACKEND))

import sqlalchemy as sa  # noqa: E402

from app.models import Incident, PaymentEvent  # noqa: E402
from app.schemas.detection import DetectionRunRequest  # noqa: E402
from app.services.detection.engine import run_detection  # noqa: E402
from app.services.detection.series import latest_event_anchor  # noqa: E402
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

# ---------------------------------------------------------------------------
# Experiment configuration (recorded verbatim in config.json)
# ---------------------------------------------------------------------------

SEED_START = 5000
N_SEEDS = 72  # 72 = 4 scenarios x 6 scales x 3 replicates
CUSTOMERS = 1_500
OUT_CSV = BACKEND / "artifacts" / "prod_frames_v1.csv"
DATASET_VERSION = "prod_frames_v1"

_SCENARIO_CYCLE = ("standard", "storm", "upi_outage_demo", "payday_wave_demo")

# Traffic-density cycle (same rationale as export_training: inference-time
# density spans ~2k-30k events/day; a single density reads the other end as
# out-of-distribution — measured failure documented in docs/ml.md §8).
_SCALE_CYCLE = (
    (15, 30_000),
    (10, 20_000),
    (2, 60_000),
    (5, 40_000),
    (2, 30_000),
    (7, 25_000),
)


def _git_sha() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=10,
    )
    return out.stdout.strip() or "unknown"


def _jitter_incidents(config, seed: int):
    """Same coverage motivations as export_training (docs/ml.md §8), own
    seeded namespace: severity jitter (mild incidents near the detection
    floor are the hard, realistic cases) + placement jitter (otherwise
    identical day-fractions band whole classes into one temporal-split
    block — measured support-0 classes on the first exact-span attempt)."""
    rng = random.Random(f"prodframe-jitter:{seed}")

    def _jitter(spec):
        params = dict(spec.params)
        if "fail_boost" in params:
            params["fail_boost"] = round(
                min(0.95, max(0.08, params["fail_boost"] * rng.uniform(0.4, 1.3))), 4
            )
        if "latency_multiplier" in params:
            params["latency_multiplier"] = round(
                max(1.0, params["latency_multiplier"] * rng.uniform(0.7, 1.5)), 4
            )
        if "abandon_boost" in params:
            params["abandon_boost"] = round(
                min(0.9, max(0.1, params["abandon_boost"] * rng.uniform(0.5, 1.4))), 4
            )
        return dataclasses.replace(
            spec,
            day_fraction=min(0.95, max(0.05, spec.day_fraction + rng.uniform(-0.08, 0.08))),
            start_hour_ist=(spec.start_hour_ist + rng.uniform(-2.0, 2.0)) % 24,
            params=params,
        )

    jittered = tuple(_jitter(spec) for spec in config.incidents)
    max_hours = config.days * 24 * 0.5
    jittered = tuple(s for s in jittered if s.duration_hours <= max_hours)
    return dataclasses.replace(config, incidents=jittered)


def collect_seed_rows(seed: int, idx: int, base_end: datetime) -> tuple[list[dict], dict]:
    """Run one seeded scenario + scheduled detection; return (rows, stats)."""
    scenario = _SCENARIO_CYCLE[seed % len(_SCENARIO_CYCLE)]
    days, events = _SCALE_CYCLE[idx % len(_SCALE_CYCLE)]
    base = SCENARIOS[scenario][1]()
    config = dataclasses.replace(
        base, seed=seed, days=days, target_events=events, customers=CUSTOMERS
    )
    # Stagger the simulated window per seed (37h stride, export convention):
    # without it the temporal split bands whole classes into single blocks.
    config = dataclasses.replace(config, end_date=base_end - timedelta(hours=37 * idx))
    config = _jitter_incidents(config, seed)

    stats = {
        "seed": seed,
        "scenario": scenario,
        "days": days,
        "target_events": events,
        "detection_passes": 0,
        "anomalies_filtered": 0,
        "incidents_persisted": 0,
    }
    rows: list[dict] = []
    with _ScratchDb() as scratch:
        db = scratch.session
        sim = run_simulation(config, db)
        gt = load_ground_truth(db, sim.run_id)
        gt_spans = [
            GroundTruthSpan(entity_id=g.entity_id, cause=g.cause, start=g.start, end=g.end)
            for g in gt
        ]

        # Scheduled passes: the production schedule (harness _detect) — a
        # pass every STEP over the simulated span, each looking back WINDOW.
        anchor = latest_event_anchor(db)
        if anchor is None:
            stats["detail"] = "no terminal events"
            return rows, stats
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
            stats["detection_passes"] += 1
            stats["anomalies_filtered"] += result.anomalies_filtered
            as_of += step

        incidents = list(db.scalars(sa.select(Incident)))
        stats["incidents_persisted"] = len(incidents)
        for inc in incidents:
            span_start, span_end = evidence_span(inc.meta, inc.window_start, inc.window_end)
            decision = label_detection_window(span_start, span_end, gt_spans)
            # Features EXACTLY as DiagnosisService.classify computes them:
            # the persisted analysis window + equal-length preceding baseline.
            features = compute_features_for_incident(db, inc)
            rows.append(
                {
                    **{name: features[name] for name in FEATURE_NAMES},
                    "label": decision.label,
                    "window_start": inc.window_start.isoformat(timespec="seconds"),
                    "window_end": inc.window_end.isoformat(timespec="seconds"),
                    # metadata (not training features — audit/analysis only)
                    "seed": seed,
                    "scenario": scenario,
                    "incident_id": inc.id,
                    "metric": inc.metric,
                    "detector": inc.detection_method,
                    "deviation_pct": inc.deviation_pct,
                    "matched_entity_id": decision.matched_entity_id or "",
                    "overlap_seconds": decision.overlap_seconds,
                    "overlapping_entity_ids": "|".join(decision.overlapping_entity_ids),
                }
            )
    return rows, stats


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed-start", type=int, default=SEED_START)
    p.add_argument("--n-seeds", type=int, default=N_SEEDS)
    p.add_argument("--out", type=Path, default=OUT_CSV)
    p.add_argument("--dataset-version", default=DATASET_VERSION)
    p.add_argument(
        "--record-dir",
        type=Path,
        default=None,
        help="where config.json/dataset_summary.json go (default: the script's own dir)",
    )
    args = p.parse_args()

    exp_dir = args.record_dir or Path(__file__).resolve().parent
    base_end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    started = datetime.now(timezone.utc)

    all_rows: list[dict] = []
    seed_stats: list[dict] = []
    seeds = [args.seed_start + i for i in range(args.n_seeds)]
    for idx, seed in enumerate(seeds):
        rows, stats = collect_seed_rows(seed, idx, base_end)
        all_rows.extend(rows)
        seed_stats.append(stats)
        print(
            f"[{idx + 1:>2}/{len(seeds)}] seed={seed} {stats['scenario']:<18} "
            f"{stats['days']}d/{stats['target_events']} ev -> "
            f"{stats['incidents_persisted']} incidents, {len(rows)} rows",
            flush=True,
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        *FEATURE_NAMES,
        "label",
        "window_start",
        "window_end",
        "seed",
        "scenario",
        "incident_id",
        "metric",
        "detector",
        "deviation_pct",
        "matched_entity_id",
        "overlap_seconds",
        "overlapping_entity_ids",
    ]
    import csv

    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    label_dist = Counter(r["label"] for r in all_rows)
    summary = {
        "dataset_version": args.dataset_version,
        "labeling_rule_version": LABELING_RULE_VERSION,
        "rows": len(all_rows),
        "label_distribution": dict(sorted(label_dist.items())),
        "no_fault_share": round(label_dist.get("no_fault", 0) / max(len(all_rows), 1), 4),
        "per_scenario_rows": dict(
            sorted(Counter(r["scenario"] for r in all_rows).items())
        ),
        "multi_overlap_rows": sum(1 for r in all_rows if "|" in r["overlapping_entity_ids"]),
        "seed_stats": seed_stats,
    }
    config = {
        "experiment": "exp01_prod_frames_dataset",
        "script": str(Path(__file__).relative_to(REPO_ROOT)),
        "argv": sys.argv,
        "dataset_version": args.dataset_version,
        "labeling_rule_version": LABELING_RULE_VERSION,
        "git_sha": _git_sha(),
        "started_utc": started.isoformat(),
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "base_end_anchor_utc": base_end.isoformat(),
        "seeds": {"start": args.seed_start, "count": args.n_seeds, "list": seeds},
        "scenario_cycle": list(_SCENARIO_CYCLE),
        "scale_cycle_days_events": [list(s) for s in _SCALE_CYCLE],
        "customers": CUSTOMERS,
        "detection": {
            "step_minutes": DETECTION_STEP_MINUTES,
            "window_minutes": DETECTION_WINDOW_MINUTES,
            "request_defaults": "DetectionRunRequest defaults (zscore, 5min buckets, "
            "noise floors 0.05/75ms, min_flagged_volume 15, min_flagged_run 2, "
            "dedup_cooldown 360, suppress_after_resolve 720) — production config, "
            "detection engine UNMODIFIED",
        },
        "jitter": {
            "namespace": "prodframe-jitter:{seed}",
            "fail_boost": "x U(0.4, 1.3) clamped [0.08, 0.95]",
            "latency_multiplier": "x U(0.7, 1.5) floored 1.0",
            "abandon_boost": "x U(0.5, 1.4) clamped [0.1, 0.9]",
            "day_fraction": "+ U(-0.08, 0.08) clamped [0.05, 0.95]",
            "start_hour_ist": "+ U(-2, 2) mod 24",
            "stagger_hours_per_seed": 37,
        },
        "output_csv": str(args.out.resolve().relative_to(REPO_ROOT)),
    }
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (exp_dir / "dataset_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nwrote {len(all_rows)} rows to {OUT_CSV}")
    print("label distribution:", dict(sorted(label_dist.items())))
    print(f"no_fault share: {summary['no_fault_share']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

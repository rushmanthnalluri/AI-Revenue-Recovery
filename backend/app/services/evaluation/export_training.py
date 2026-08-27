"""Export labeled diagnosis training windows from real simulator output.

For each seed, a scratch SQLite DB is seeded with a simulator scenario
(cycling standard / storm / upi_outage_demo / payday_wave_demo for diversity);
labeled feature rows are then computed with the SAME feature code used at
inference time (``compute_features_for_incident``):

- positives: one row per ``simulator_ground_truth`` incident window, labeled
  by ``truth_cause`` (the IncidentKind -> CauseLabel mapping, shared with the
  evaluation harness);
- negatives: quiet windows sampled away from any injected incident (12h
  safety margin), labeled ``no_fault`` — ``negatives_per_positive`` per run.

Output columns: the 58 FEATURE_NAMES + label + window_start/window_end +
metadata (seed, scenario, entity_id) — the frame contract of
``scripts/train_models.py --input``.

Run from backend/:

    .venv/Scripts/python -m app.services.evaluation.export_training \
        --out artifacts/sim_features.csv --seeds 20
"""

import argparse
import csv
import random
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa

from app.services.diagnosis.features import (
    FEATURE_NAMES,
    compute_features_for_incident,
)
from app.services.diagnosis.taxonomy import CauseLabel
from app.services.evaluation.runner import _ScratchDb, load_ground_truth, truth_cause
from app.simulator.config import SCENARIOS
from app.simulator.engine import run_simulation

_SCENARIO_CYCLE = ("standard", "storm", "upi_outage_demo", "payday_wave_demo")

# (days, target_events) per seed index. Traffic DENSITY must span the range
# the model meets at inference: the demo scenarios run ~30k events/day while
# the evaluation presets run ~2k/day — a model trained at one density reads
# the other as out-of-distribution (measured: a 15d/30k-only model labels a
# 2d/60k gateway degradation as subscription_failure_spike).
_SCALE_CYCLE = (
    (15, 30_000),
    (10, 20_000),
    (2, 60_000),
    (5, 40_000),
    (2, 30_000),
    (7, 25_000),
)


def _base_end() -> datetime:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def export_rows(
    *,
    seeds: list[int],
    days: int | None,
    events: int | None,
    customers: int,
    negatives_per_positive: int,
) -> list[dict]:
    import dataclasses

    rows: list[dict] = []
    for idx, seed in enumerate(seeds):
        scenario = _SCENARIO_CYCLE[seed % len(_SCENARIO_CYCLE)]
        scale_days, scale_events = _SCALE_CYCLE[idx % len(_SCALE_CYCLE)]
        base = SCENARIOS[scenario][1]()
        config = dataclasses.replace(
            base,
            seed=seed,
            days=days or scale_days,
            target_events=events or scale_events,
            customers=customers,
        )
        # Stagger the simulated window per seed: without it, a given incident
        # kind sits at the same absolute window_end across seeds (same
        # day-fractions + same end anchor), so the trainer's temporal split
        # bands whole classes into single blocks (measured: support-0 classes
        # in the held-out test block). A 37h stride scatters every class
        # across the full timeline. day_fraction/start_hour jitter adds
        # within-window diversity on top.
        end_date = _base_end() - timedelta(hours=37 * idx)
        config = dataclasses.replace(config, end_date=end_date)
        # Jitter incident placement per seed: without it every seed of a
        # scenario injects at identical day-fractions, whole classes share
        # window_end timestamps, and the trainer's temporal split silently
        # puts entire classes into one block (measured: support-0 classes).
        jitter_rng = random.Random(f"training-export-jitter:{seed}")

        def _jitter_spec(spec):
            params = dict(spec.params)
            # Severity coverage: presets only inject STRONG incidents
            # (fail_boost >= 0.35); real and demo degradations include mild
            # ones (fail_boost ~0.10). Jitter the strength so the model
            # learns the mild end instead of interpolating to no_fault.
            if "fail_boost" in params:
                params["fail_boost"] = round(
                    min(0.95, max(0.08, params["fail_boost"] * jitter_rng.uniform(0.4, 1.3))), 4
                )
            if "latency_multiplier" in params:
                params["latency_multiplier"] = round(
                    max(1.0, params["latency_multiplier"] * jitter_rng.uniform(0.7, 1.5)), 4
                )
            if "abandon_boost" in params:
                params["abandon_boost"] = round(
                    min(0.9, max(0.1, params["abandon_boost"] * jitter_rng.uniform(0.5, 1.4))), 4
                )
            return dataclasses.replace(
                spec,
                day_fraction=min(0.95, max(0.05, spec.day_fraction + jitter_rng.uniform(-0.08, 0.08))),
                start_hour_ist=(spec.start_hour_ist + jitter_rng.uniform(-2.0, 2.0)) % 24,
                params=params,
            )

        jittered = tuple(_jitter_spec(spec) for spec in config.incidents)
        # Drop incidents whose span cannot fit the (possibly short) window.
        max_hours = config.days * 24 * 0.5
        jittered = tuple(s for s in jittered if s.duration_hours <= max_hours)
        config = dataclasses.replace(config, incidents=jittered)
        with _ScratchDb() as scratch:
            db = scratch.session
            sim = run_simulation(config, db)
            gt = load_ground_truth(db, sim.run_id)

            for g in gt:
                # Window-shape invariance: at inference time the feature
                # window is the DETECTION analysis window, not the exact
                # ground-truth span, and its anchor can fall anywhere around
                # the incident. Emit the exact span plus randomized frames
                # (pre-pad 0..90min, post-pad 0..60min, 5-minute grid,
                # seeded per incident) so the model learns the continuum of
                # detection anchorings instead of the ground-truth edges
                # (measured: span-only and fixed-variant models mislabel
                # real detection windows).
                frame_rng = random.Random(f"training-export-frame:{seed}:{g.entity_id}")
                variants = [(timedelta(0), timedelta(0))]
                for _ in range(4):
                    if frame_rng.random() < 0.5:
                        # tight frame: detection anchored near the incident
                        pre = timedelta(minutes=5 * frame_rng.randint(0, 18))
                        post = timedelta(minutes=5 * frame_rng.randint(0, 12))
                    else:
                        # wide frame: scheduled 12h-wide detection passes
                        # produce windows where the anomaly is a minority of
                        # the span — the model must see those too
                        pre = timedelta(minutes=30 * frame_rng.randint(0, 16))
                        post = timedelta(minutes=30 * frame_rng.randint(0, 16))
                    variants.append((-pre, post))
                for v_idx, (start_off, end_off) in enumerate(variants):
                    w_start = g.start + start_off
                    w_end = g.end + end_off
                    features = compute_features_for_incident(
                        db,
                        SimpleNamespace(
                            id=f"{g.entity_id}_v{v_idx}",
                            window_start=w_start,
                            window_end=w_end,
                            detected_at=w_end,
                        ),
                    )
                    rows.append(
                        {
                            **{name: features[name] for name in FEATURE_NAMES},
                            "label": truth_cause(
                                {"kind": g.kind, "params": _params(db, sim.run_id, g.entity_id)}
                            ),
                            "window_start": w_start.isoformat(timespec="seconds"),
                            "window_end": w_end.isoformat(timespec="seconds"),
                            "seed": seed,
                            "scenario": scenario,
                            "entity_id": f"{g.entity_id}_v{v_idx}",
                        }
                    )

            rows.extend(
                _negative_rows(
                    db,
                    seed=seed,
                    scenario=scenario,
                    gt=gt,
                    # 5 frames per incident above -> scale negatives to match
                    count=negatives_per_positive * len(gt) * 5,
                    sim_start=sim.stats["window"]["start"],
                    sim_end=sim.stats["window"]["end"],
                )
            )
    return rows


def _params(db, run_id: str, entity_id: str) -> dict:
    from app.models import SimulatorGroundTruth

    row = db.scalar(
        sa.select(SimulatorGroundTruth).where(
            SimulatorGroundTruth.simulator_run_id == run_id,
            SimulatorGroundTruth.entity_type == "incident",
            SimulatorGroundTruth.entity_id == entity_id,
        )
    )
    return dict((row.truth or {}).get("params") or {}) if row else {}


def _negative_rows(
    db,
    *,
    seed: int,
    scenario: str,
    gt: list,
    count: int,
    sim_start: str,
    sim_end: str,
) -> list[dict]:
    from datetime import datetime, timezone

    start = datetime.fromisoformat(sim_start).astimezone(timezone.utc)
    end = datetime.fromisoformat(sim_end).astimezone(timezone.utc)
    margin = timedelta(hours=12)
    rng = random.Random(f"training-export:{seed}")
    out: list[dict] = []
    attempts = 0
    while len(out) < count and attempts < count * 40:
        attempts += 1
        duration = timedelta(hours=rng.choice((1.0, 2.0, 3.0, 6.0)))
        span = (end - start) - duration - 2 * margin
        w_start = (start + margin + rng.random() * span).replace(microsecond=0)
        w_end = (w_start + duration).replace(microsecond=0)
        if any(w_start < g.end + margin and g.start - margin < w_end for g in gt):
            continue
        entity_id = f"nofault_{seed}_{len(out):02d}"
        features = compute_features_for_incident(
            db,
            SimpleNamespace(
                id=entity_id, window_start=w_start, window_end=w_end, detected_at=w_end
            ),
        )
        out.append(
            {
                **{name: features[name] for name in FEATURE_NAMES},
                "label": CauseLabel.NO_FAULT.value,
                "window_start": w_start.isoformat(timespec="seconds"),
                "window_end": w_end.isoformat(timespec="seconds"),
                "seed": seed,
                "scenario": scenario,
                "entity_id": entity_id,
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="export_training", description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seeds", type=int, default=60, help="number of simulator seeds")
    p.add_argument("--seed-start", type=int, default=1000)
    p.add_argument("--days", type=int, default=None,
                   help="pin every seed to this length (default: density-diverse cycle)")
    p.add_argument("--events", type=int, default=None,
                   help="pin every seed to this size (default: density-diverse cycle)")
    p.add_argument("--customers", type=int, default=1_500)
    p.add_argument("--negatives-per-positive", type=int, default=1)
    args = p.parse_args(argv)

    seeds = [args.seed_start + i for i in range(args.seeds)]
    rows = export_rows(
        seeds=seeds,
        days=args.days,
        events=args.events,
        customers=args.customers,
        negatives_per_positive=args.negatives_per_positive,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [*FEATURE_NAMES, "label", "window_start", "window_end", "seed", "scenario", "entity_id"]
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    from collections import Counter

    print(f"wrote {len(rows)} rows to {args.out}")
    print("label distribution:", dict(sorted(Counter(r['label'] for r in rows).items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())

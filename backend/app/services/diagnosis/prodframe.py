"""Production-frame labeling: map a *detection-produced* incident window to a
diagnosis label using simulator ground truth.

Production context (the train/serve skew this closes): ``DiagnosisService``
never sees exact injected spans. It sees the windows the *detection engine*
persists — scheduled passes every 6h, each looking back 12h
(``DETECTION_STEP_MINUTES``/``DETECTION_WINDOW_MINUTES`` in the evaluation
harness, production defaults, post noise-floor calibration). Training rows
must therefore be one row per *persisted detection incident*, features
computed on exactly the window ``DiagnosisService.classify`` would use
(``incident.window_start``/``window_end``), labeled from ground truth by the
rule below.

Labeling rule (deterministic, documented, unit-tested here):

1. The incident's *evidence span* is ``meta.anomaly_start..meta.anomaly_end``
   when detection recorded one, else the analysis window
   (``window_start..window_end``) — the same precedence the evaluation
   harness uses when matching detection to ground truth.
2. A ground-truth incident *overlaps* when the two half-open spans share at
   least one second.
3. **No overlap** -> ``no_fault`` (a persisted detection that noise floors
   admitted but no injected incident explains — exactly the false-positive
   frame production must learn to absorb). The post-redesign admission
   floors (docs/detection.md) remove most organic-noise rows, so this class
   is smaller than the exact-span dataset's sampled-quiet negatives — an
   intended, measured distribution shift.
4. **One overlap** -> that incident's cause (``KIND_TO_CAUSE``; a method
   outage with a targeted bank is ``bank_downtime``).
5. **Multiple overlaps** (the storm preset injects partially concurrent
   incidents) -> the cause of the ground-truth incident with the **largest
   overlap in seconds** with the evidence span; ties break to the earliest
   ground-truth start, then entity id. All overlapping entity ids are kept
   on the row's metadata so failure analysis can audit the ambiguity.

Everything here is pure: no DB, no simulator, no detection imports — the
dataset *driver* (ml/experiments/diagnosis/) composes those; this module is
the testable contract.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.services.diagnosis.taxonomy import CauseLabel

#: Labeling-rule version — bump when the rule above changes; recorded in the
#: dataset's config so experiment records pin rule + data together.
LABELING_RULE_VERSION = "prodframe-label-v1"


@dataclass(frozen=True)
class GroundTruthSpan:
    """One simulator ground-truth incident, reduced to what labeling needs."""

    entity_id: str
    cause: str  # already mapped through KIND_TO_CAUSE / truth_cause
    start: datetime  # tz-aware UTC
    end: datetime  # tz-aware UTC


@dataclass(frozen=True)
class LabelDecision:
    """Outcome of labeling one detection-produced window."""

    label: str
    matched_entity_id: str | None  # None iff label == no_fault
    overlap_seconds: float
    overlapping_entity_ids: tuple[str, ...] = field(default=())  # audit trail


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def evidence_span(
    meta: dict[str, Any] | None,
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, datetime]:
    """The span matched against ground truth: the detected anomaly bounds when
    present, else the analysis window (harness precedence, runner._overlaps)."""
    meta = meta or {}
    try:
        start = _aware(datetime.fromisoformat(str(meta["anomaly_start"])))
        end = _aware(datetime.fromisoformat(str(meta["anomaly_end"])))
        if end > start:
            return start, end
    except (KeyError, ValueError):
        pass
    return _aware(window_start), _aware(window_end)


def _overlap_seconds(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    return max(0.0, (min(a_end, b_end) - max(a_start, b_start)).total_seconds())


def label_detection_window(
    span_start: datetime,
    span_end: datetime,
    ground_truth: list[GroundTruthSpan],
) -> LabelDecision:
    """Apply the module-level labeling rule to one evidence span."""
    span_start, span_end = _aware(span_start), _aware(span_end)
    overlaps: list[tuple[float, GroundTruthSpan]] = []
    for gt in ground_truth:
        seconds = _overlap_seconds(span_start, span_end, _aware(gt.start), _aware(gt.end))
        if seconds > 0.0:
            overlaps.append((seconds, gt))
    if not overlaps:
        return LabelDecision(
            label=CauseLabel.NO_FAULT.value,
            matched_entity_id=None,
            overlap_seconds=0.0,
            overlapping_entity_ids=(),
        )
    # Largest overlap wins; ties -> earliest gt start, then entity id.
    overlaps.sort(key=lambda item: (-item[0], item[1].start, item[1].entity_id))
    best_seconds, best = overlaps[0]
    return LabelDecision(
        label=best.cause,
        matched_entity_id=best.entity_id,
        overlap_seconds=best_seconds,
        overlapping_entity_ids=tuple(gt.entity_id for _, gt in overlaps),
    )


__all__ = [
    "LABELING_RULE_VERSION",
    "GroundTruthSpan",
    "LabelDecision",
    "evidence_span",
    "label_detection_window",
]

"""Production-frame labeling rule (app.services.diagnosis.prodframe):

evidence-span precedence (anomaly span over analysis window), the overlap
rule, the multi-incident tie-break, and the no_fault absorption class. Pure
functions — no DB, no simulator.
"""

from datetime import datetime, timedelta, timezone

from app.services.diagnosis.prodframe import (
    LABELING_RULE_VERSION,
    GroundTruthSpan,
    evidence_span,
    label_detection_window,
)

T0 = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def gt(entity_id: str, cause: str, start: datetime, end: datetime) -> GroundTruthSpan:
    return GroundTruthSpan(entity_id=entity_id, cause=cause, start=start, end=end)


class TestEvidenceSpan:
    def test_anomaly_span_preferred_over_window(self):
        meta = {
            "anomaly_start": (T0 + 2 * H).isoformat(),
            "anomaly_end": (T0 + 4 * H).isoformat(),
        }
        start, end = evidence_span(meta, T0, T0 + 12 * H)
        assert (start, end) == (T0 + 2 * H, T0 + 4 * H)

    def test_window_fallback_when_meta_missing_or_malformed(self):
        assert evidence_span({}, T0, T0 + 12 * H) == (T0, T0 + 12 * H)
        assert evidence_span(None, T0, T0 + 12 * H) == (T0, T0 + 12 * H)
        bad = {"anomaly_start": "not-a-date", "anomaly_end": "also-not"}
        assert evidence_span(bad, T0, T0 + 12 * H) == (T0, T0 + 12 * H)
        inverted = {
            "anomaly_start": (T0 + 4 * H).isoformat(),
            "anomaly_end": (T0 + 2 * H).isoformat(),
        }
        assert evidence_span(inverted, T0, T0 + 12 * H) == (T0, T0 + 12 * H)

    def test_naive_datetimes_treated_as_utc(self):
        naive = datetime(2026, 8, 1, 0, 0, 0)
        start, end = evidence_span(None, naive, naive + H)
        assert start.tzinfo is not None and end.tzinfo is not None


class TestLabelDetectionWindow:
    def test_no_overlap_is_no_fault(self):
        truth = [gt("inc_1", "gateway_degradation", T0, T0 + 2 * H)]
        decision = label_detection_window(T0 + 6 * H, T0 + 18 * H, truth)
        assert decision.label == "no_fault"
        assert decision.matched_entity_id is None
        assert decision.overlap_seconds == 0.0
        assert decision.overlapping_entity_ids == ()

    def test_half_open_boundary_does_not_overlap(self):
        truth = [gt("inc_1", "method_outage", T0, T0 + 2 * H)]
        decision = label_detection_window(T0 + 2 * H, T0 + 14 * H, truth)
        assert decision.label == "no_fault"

    def test_single_overlap_labels_by_cause(self):
        truth = [gt("inc_1", "bank_downtime", T0 + H, T0 + 3 * H)]
        decision = label_detection_window(T0, T0 + 12 * H, truth)
        assert decision.label == "bank_downtime"
        assert decision.matched_entity_id == "inc_1"
        assert decision.overlap_seconds == 2 * 3600.0
        assert decision.overlapping_entity_ids == ("inc_1",)

    def test_multi_overlap_largest_overlap_wins(self):
        # span covers 30min of inc_small and 3h of inc_large -> inc_large.
        truth = [
            gt("inc_small", "no_fault_irrelevant", T0, T0 + timedelta(minutes=30)),
            gt("inc_large", "gateway_degradation", T0 + 2 * H, T0 + 5 * H),
        ]
        truth[0] = gt("inc_small", "method_outage", T0, T0 + timedelta(minutes=30))
        decision = label_detection_window(T0, T0 + 12 * H, truth)
        assert decision.label == "gateway_degradation"
        assert decision.matched_entity_id == "inc_large"
        assert decision.overlap_seconds == 3 * 3600.0
        # both overlapping ids kept for the audit trail
        assert set(decision.overlapping_entity_ids) == {"inc_small", "inc_large"}

    def test_multi_overlap_tie_breaks_to_earliest_start(self):
        truth = [
            gt("inc_late", "method_outage", T0 + 4 * H, T0 + 6 * H),
            gt("inc_early", "gateway_degradation", T0, T0 + 2 * H),
        ]
        decision = label_detection_window(T0, T0 + 12 * H, truth)  # 2h overlap each
        assert decision.matched_entity_id == "inc_early"
        assert decision.label == "gateway_degradation"

    def test_naive_span_datetimes_treated_as_utc(self):
        truth = [gt("inc_1", "method_outage", T0, T0 + 2 * H)]
        decision = label_detection_window(
            datetime(2026, 8, 1, 1, 0, 0), datetime(2026, 8, 1, 13, 0, 0), truth
        )
        assert decision.label == "method_outage"


def test_labeling_rule_version_pinned():
    # The dataset config records this version; bump deliberately, never silently.
    assert LABELING_RULE_VERSION == "prodframe-label-v1"

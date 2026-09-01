"""Incident-level noise floors, cross-pass episode dedup, and post-resolution
suppression — the precision machinery from docs/detection.md.

These tests run the full engine via the API on seeded payment_events (exact
rates, no randomness) and assert on persisted rows, so they cover floors +
persistence together.
"""

from datetime import timedelta

import sqlalchemy as sa

from app.db import utcnow
from app.models import Incident
from app.ports import IncidentStatus
from app.services.detection.engine import (
    DEFAULT_MIN_ABSOLUTE_DEVIATION,
    _flagged_run_and_volume,
)
from app.services.detection.detectors import Anomaly
from app.services.detection.series import Bucket, floor_bucket, latest_event_anchor
from tests.detection.conftest import EPOCH, Stream

RUN_BODY = {
    "window_minutes": 240,
    "bucket_minutes": 5,
    "detector": "zscore",
    "metrics": ["payment_success_rate"],
    "baseline_buckets": 12,
    "min_bucket_count": 5,
    # seeded fixtures are simulator-provenance: run in the research environment
    "environment": "research",
}


def _sustained_drop(i: int) -> float:
    return 0.9 if i < 24 else 0.4  # -55.6% for the whole second half


def _one_blip(i: int) -> float:
    return 0.4 if i == 30 else 0.9  # a single bad bucket — a blip, not an incident


def _n_incidents(db_session) -> int:
    return db_session.scalar(sa.select(sa.func.count()).select_from(Incident))


class TestNoiseFloors:
    def test_single_bucket_blip_is_filtered(self, client, db_session, seed_payment_events):
        seed_payment_events(streams=[Stream(rate_at=_one_blip)])
        r = client.post("/api/v1/detection/run", json=RUN_BODY)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["anomalies_detected"] == 0
        assert body["anomalies_filtered"] == 1  # fired, but failed the floors
        assert body["incidents"] == []
        assert _n_incidents(db_session) == 0

    def test_floors_are_request_configurable(self, client, db_session, seed_payment_events):
        """The same blip becomes an incident when the caller relaxes the
        floors — the defaults, not the detector, changed."""
        seed_payment_events(streams=[Stream(rate_at=_one_blip)])
        r = client.post(
            "/api/v1/detection/run",
            json={**RUN_BODY, "min_flagged_run": 1, "min_flagged_volume": 0},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["anomalies_detected"] == 1
        assert body["anomalies_filtered"] == 0
        assert _n_incidents(db_session) == 1

    def test_absolute_deviation_floor(self, client, db_session, seed_payment_events):
        """A sustained 4pp drop (0.90 -> 0.86) fires the z-score (baseline std
        is floored at 1% of mean) but is below the 5pp absolute floor."""
        seed_payment_events(
            streams=[
                Stream(per_bucket=100, rate_at=lambda i: 0.9 if i < 24 else 0.86)
            ]
        )
        r = client.post("/api/v1/detection/run", json=RUN_BODY)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["anomalies_detected"] == 0
        assert body["anomalies_filtered"] == 1
        assert _n_incidents(db_session) == 0
        # and with the floor explicitly disabled it persists
        r2 = client.post(
            "/api/v1/detection/run",
            json={**RUN_BODY, "min_absolute_deviation": 0.0},
        )
        assert r2.json()["anomalies_detected"] == 1

    def test_per_metric_default_floors_exist(self):
        assert DEFAULT_MIN_ABSOLUTE_DEVIATION["payment_success_rate"] == 0.05
        assert DEFAULT_MIN_ABSOLUTE_DEVIATION["capture_latency_ms"] == 75.0

    def test_invalid_floor_values_rejected_422(self, client):
        r = client.post("/api/v1/detection/run", json={"min_flagged_run": 0})
        assert r.status_code == 422
        r = client.post("/api/v1/detection/run", json={"min_flagged_volume": -1})
        assert r.status_code == 422
        r = client.post("/api/v1/detection/run", json={"min_absolute_deviation": -0.5})
        assert r.status_code == 422

    def test_sustained_drop_still_detected(self, client, db_session, seed_payment_events):
        """Guard against over-filtering: a real sustained degradation clears
        all floors with the production defaults."""
        seed_payment_events(streams=[Stream(rate_at=_sustained_drop)])
        body = client.post("/api/v1/detection/run", json=RUN_BODY).json()
        assert body["anomalies_detected"] == 1
        assert body["anomalies_filtered"] == 0
        assert len(body["incidents_created"]) == 1


class TestFlaggedRunAndVolume:
    def _series(self, counts=(20,) * 10):
        return [
            Bucket(ts=EPOCH + timedelta(minutes=5 * i), value=0.9, count=counts[i])
            for i in range(len(counts))
        ]

    def _anomaly(self, flagged):
        return Anomaly(
            detector="zscore",
            start_ts=flagged[0],
            end_ts=flagged[-1],
            baseline=0.9,
            observed=0.4,
            deviation_pct=-55.0,
            score=5.0,
            flagged_ts=tuple(flagged),
        )

    def test_longest_consecutive_run(self):
        series = self._series()
        flagged = [series[1].ts, series[2].ts, series[3].ts, series[7].ts]
        longest, volume = _flagged_run_and_volume(self._anomaly(flagged), series)
        assert longest == 3  # buckets 1-2-3, then a gap, then bucket 7 alone
        assert volume == 80

    def test_sparse_gaps_break_the_run(self):
        series = self._series()
        flagged = [series[2].ts, series[4].ts]
        longest, volume = _flagged_run_and_volume(self._anomaly(flagged), series)
        assert longest == 1
        assert volume == 40


class TestCrossPassEpisodeDedup:
    def test_overlapping_pass_merges_instead_of_duplicating(
        self, client, db_session, seed_payment_events
    ):
        """A second pass whose window overlaps the same episode must refresh
        the open incident, not open a second one; detected_at (MTTD) and the
        window bounds stay with the first detection."""
        seed_payment_events(streams=[Stream(rate_at=_sustained_drop)])
        first = client.post("/api/v1/detection/run", json=RUN_BODY).json()
        inc_id = first["incidents_created"][0]
        inc = db_session.get(Incident, inc_id)
        detected_at, window_start = inc.detected_at, inc.window_start

        anchor = latest_event_anchor(db_session)
        shifted = {**RUN_BODY, "as_of": (anchor - timedelta(minutes=30)).isoformat()}
        second = client.post("/api/v1/detection/run", json=shifted).json()
        assert second["incidents_created"] == []
        assert second["incidents_updated"] == [inc_id]
        view = second["incidents"][0]
        assert view["action"] == "updated"
        assert "merged" in (view["detail"] or "")

        assert _n_incidents(db_session) == 1
        inc = db_session.get(Incident, inc_id)
        assert inc.detected_at == detected_at  # honest MTTD
        assert inc.window_start == window_start  # first window owns the episode
        assert inc.status == IncidentStatus.OPEN
        assert inc.meta["merge_count"] == 1

    def test_dry_run_reports_would_update_on_merge(
        self, client, db_session, seed_payment_events
    ):
        seed_payment_events(streams=[Stream(rate_at=_sustained_drop)])
        client.post("/api/v1/detection/run", json=RUN_BODY)
        anchor = latest_event_anchor(db_session)
        shifted = {
            **RUN_BODY,
            "as_of": (anchor - timedelta(minutes=30)).isoformat(),
            "dry_run": True,
        }
        body = client.post("/api/v1/detection/run", json=shifted).json()
        assert body["incidents"][0]["action"] == "would_update"

    def test_distinct_episodes_beyond_cooldown_stay_separate(
        self, client, db_session, seed_payment_events
    ):
        """Two drops ~5h apart with a 60-min cooldown are two episodes."""
        seed_payment_events(
            buckets=96,
            streams=[
                Stream(
                    rate_at=lambda i: 0.4
                    if (12 <= i < 24) or i >= 84
                    else 0.9
                )
            ],
        )
        anchor = latest_event_anchor(db_session)
        pass1 = {
            **RUN_BODY,
            "as_of": (anchor - timedelta(minutes=240)).isoformat(),
            "dedup_cooldown_minutes": 60,
        }
        pass2 = {**RUN_BODY, "dedup_cooldown_minutes": 60}
        first = client.post("/api/v1/detection/run", json=pass1).json()
        assert len(first["incidents_created"]) == 1
        second = client.post("/api/v1/detection/run", json=pass2).json()
        assert len(second["incidents_created"]) == 1
        assert _n_incidents(db_session) == 2

    def test_same_episode_within_default_cooldown_merges(
        self, client, db_session, seed_payment_events
    ):
        """Same data as above but with the default 360-min cooldown: the
        ~305-minute gap between the two episode spans is inside the cooldown,
        so the second pass merges into the first episode instead of creating
        a new incident."""
        seed_payment_events(
            buckets=96,
            streams=[
                Stream(
                    rate_at=lambda i: 0.4
                    if (12 <= i < 24) or i >= 84
                    else 0.9
                )
            ],
        )
        anchor = latest_event_anchor(db_session)
        pass1 = {**RUN_BODY, "as_of": (anchor - timedelta(minutes=240)).isoformat()}
        client.post("/api/v1/detection/run", json=pass1)
        second = client.post("/api/v1/detection/run", json=RUN_BODY).json()
        # default cooldown 360 >= the ~305-minute gap between the two spans
        assert second["incidents_created"] == []
        assert _n_incidents(db_session) == 1

    def test_merging_disabled_returns_to_per_window_upsert(
        self, client, db_session, seed_payment_events
    ):
        seed_payment_events(streams=[Stream(rate_at=_sustained_drop)])
        client.post("/api/v1/detection/run", json=RUN_BODY)
        anchor = latest_event_anchor(db_session)
        shifted = {
            **RUN_BODY,
            "as_of": (anchor - timedelta(minutes=30)).isoformat(),
            "dedup_cooldown_minutes": None,
        }
        body = client.post("/api/v1/detection/run", json=shifted).json()
        assert len(body["incidents_created"]) == 1  # a second row, legacy style
        assert _n_incidents(db_session) == 2


class TestPostResolutionSuppression:
    def _resolve(self, db_session, incident_id: str, resolved_at):
        inc = db_session.get(Incident, incident_id)
        inc.status = IncidentStatus.RESOLVED
        inc.resolved_at = resolved_at
        db_session.commit()

    def test_redetection_within_window_is_suppressed(
        self, client, db_session, seed_payment_events
    ):
        seed_payment_events(streams=[Stream(rate_at=_sustained_drop)])
        first = client.post("/api/v1/detection/run", json=RUN_BODY).json()
        inc_id = first["incidents_created"][0]
        self._resolve(db_session, inc_id, utcnow())

        anchor = latest_event_anchor(db_session)
        shifted = {**RUN_BODY, "as_of": (anchor - timedelta(minutes=30)).isoformat()}
        second = client.post("/api/v1/detection/run", json=shifted).json()
        assert second["anomalies_detected"] == 0
        assert second["anomalies_filtered"] == 1
        assert second["incidents_created"] == []
        view = second["incidents"][0]
        assert view["action"] == "suppressed"
        assert view["incident_id"] == inc_id
        assert "suppress" in (view["detail"] or "")

        assert _n_incidents(db_session) == 1  # nothing new persisted
        inc = db_session.get(Incident, inc_id)
        assert inc.status == IncidentStatus.RESOLVED  # resolution not clobbered

    def test_redetection_after_window_creates_new_incident(
        self, client, db_session, seed_payment_events
    ):
        seed_payment_events(streams=[Stream(rate_at=_sustained_drop)])
        first = client.post("/api/v1/detection/run", json=RUN_BODY).json()
        inc_id = first["incidents_created"][0]
        # resolved long enough ago that the suppression window has expired:
        # the rule compares the NEW anomaly's start (~2h back inside this
        # window) against resolved_at + 12h, so 15h puts us past it
        self._resolve(db_session, inc_id, utcnow() - timedelta(hours=15))

        anchor = latest_event_anchor(db_session)
        shifted = {**RUN_BODY, "as_of": (anchor - timedelta(minutes=30)).isoformat()}
        second = client.post("/api/v1/detection/run", json=shifted).json()
        assert len(second["incidents_created"]) == 1
        assert _n_incidents(db_session) == 2

    def test_suppression_disabled_reopens(
        self, client, db_session, seed_payment_events
    ):
        seed_payment_events(streams=[Stream(rate_at=_sustained_drop)])
        first = client.post("/api/v1/detection/run", json=RUN_BODY).json()
        self._resolve(db_session, first["incidents_created"][0], utcnow())

        anchor = latest_event_anchor(db_session)
        shifted = {
            **RUN_BODY,
            "as_of": (anchor - timedelta(minutes=30)).isoformat(),
            "suppress_after_resolve_minutes": None,
        }
        second = client.post("/api/v1/detection/run", json=shifted).json()
        assert len(second["incidents_created"]) == 1
        assert _n_incidents(db_session) == 2

    def test_exact_window_rerun_still_updates_resolved_incident(
        self, client, db_session, seed_payment_events
    ):
        """Legacy idempotency is preserved: an identical re-run UPDATEs the
        (resolved) row in place — suppression applies to re-detection from a
        different window, not to a byte-identical replay."""
        seed_payment_events(streams=[Stream(rate_at=_sustained_drop)])
        first = client.post("/api/v1/detection/run", json=RUN_BODY).json()
        inc_id = first["incidents_created"][0]
        self._resolve(db_session, inc_id, utcnow())

        second = client.post("/api/v1/detection/run", json=RUN_BODY).json()
        assert second["incidents_updated"] == [inc_id]
        assert _n_incidents(db_session) == 1
        assert db_session.get(Incident, inc_id).status == IncidentStatus.RESOLVED


def test_floor_bucket_used_by_tests():
    # keeps the floor_bucket import honest: the seed grid is bucket-aligned
    assert floor_bucket(utcnow(), 5).minute % 5 == 0

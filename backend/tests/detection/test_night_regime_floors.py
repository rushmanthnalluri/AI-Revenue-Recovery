"""Night-regime floors for ``insufficient_fund_share``
(DetectionRunRequest.night_regime_floors).

The published operating point holds every anomaly to one global floor set —
including the 0.90 ``min_observed`` share bar that exists only because
measured organic DAYTIME insufficient-fund clusters reach 0.71 share. That
bar is exactly why ``customer_insufficient_funds_wave`` goes 0/7 on the
published anchors: the wave's failures spread across night-trough buckets
and never concentrate into a near-single-class hour (docs/evaluation.md
§3b note 2). The opt-in ``night_regime_floors`` mode judges an anomaly
whose flagged buckets ALL sit in the night band (18:00–01:00 UTC, the IST
night trough) by a lower share/absolute floor set; anything touching a
daytime bucket faces the global floors in both modes. Default OFF, so the
published anchors stay valid.

Everything here is deterministic: exact terminal failures per hour on an
explicit one-day grid (no randomness, no wall-clock dependence — the window
is anchored by an explicit `as_of`).

Fixture geometry: window [02:00, 22:00) UTC on one day, 60-minute metric
buckets. The detector baseline is the first 8 valid buckets (02:00–09:00);
scored buckets run 10:00–21:00, of which 18:00–21:00 sit in the night band.
"""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

import app.models as models
from app.schemas.detection import DetectionRunRequest
from app.services.detection import run_detection

DAY = datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)
AS_OF = DAY.replace(hour=21, minute=55)  # window_end = 22:00, window = [02:00, 22:00)
METRIC = "insufficient_fund_share"

#: Healthy organic baseline: 1 insufficient-funds failure out of 4 per hour
#: (share 0.25) for every baseline hour 02:00–09:00.
HEALTHY_BASELINE = {h: (1, 3) for h in range(2, 10)}


def _seed(db_session, merchant, hourly: dict[int, tuple[int, int]]) -> None:
    """Terminal failures at hh:05 UTC for each hour in [02:00, 22:00).

    ``hourly`` maps hour -> (n_insufficient_fund_failures, n_other_failures);
    hours absent from the map get no events (their buckets carry value=None
    and are skipped by the detectors).
    """
    for hour, (n_if, n_other) in sorted(hourly.items()):
        ts = DAY.replace(hour=hour, minute=5)
        for j in range(n_if + n_other):
            reason = "insufficient_funds" if j < n_if else "card_declined"
            payment = models.Payment(
                merchant_id=merchant.id,
                amount_paise=10_000,
                status="failed",
                gateway_created_at=ts,
            )
            db_session.add(payment)
            db_session.flush()
            db_session.add(
                models.PaymentEvent(
                    payment_id=payment.id,
                    event_type="payment.failed",
                    to_status="failed",
                    source="seed",
                    payload={"error_reason": reason},
                    occurred_at=ts,
                )
            )


def _run(db_session, *, night_regime_floors: bool) -> tuple:
    result = run_detection(
        db_session,
        DetectionRunRequest(
            as_of=AS_OF,
            window_minutes=20 * 60,
            metrics=[METRIC],
            environment="research",
            night_regime_floors=night_regime_floors,
        ),
    )
    incidents = list(db_session.scalars(sa.select(models.Incident)))
    return result, incidents


def _merchant(db_session, name: str):
    merchant = models.Merchant(name=name)
    db_session.add(merchant)
    db_session.flush()
    return merchant


class TestNightWave:
    """The wave shape from §3b note 2: diluted all-night buckets (share
    0.667 — under the global 0.90 bar, over the 0.60 night bar)."""

    def _fixture(self, db_session, name: str) -> None:
        merchant = _merchant(db_session, name)
        _seed(
            db_session,
            merchant,
            {
                **HEALTHY_BASELINE,
                18: (4, 2),  # night wave: share 0.667 in every flagged bucket
                19: (4, 2),
                20: (4, 2),
                21: (1, 3),
            },
        )
        db_session.commit()

    def test_night_wave_detected_with_mode_on(self, db_session):
        self._fixture(db_session, "night wave on")

        result, incidents = _run(db_session, night_regime_floors=True)

        assert result.anomalies_detected == 1
        (incident,) = [i for i in incidents if i.metric == METRIC]
        assert incident.baseline_value == 0.25
        assert incident.observed_value == round(4 / 6, 6)
        assert incident.meta["night_regime_floors"] is True
        assert "night_regime_floors=on" in (result.detail or "")

    def test_same_series_stays_silent_with_mode_off(self, db_session):
        """Control: the SAME fixture must stay silent under the global floor
        set — otherwise the mode-on test above proves nothing. This is the
        0/7 anchor mechanism reproduced on a synthetic series."""
        self._fixture(db_session, "night wave off")

        result, incidents = _run(db_session, night_regime_floors=False)

        # The detector fires (z >> 3); the global 0.90 min_observed bar
        # rejects it.
        assert result.anomalies_detected == 0
        assert result.anomalies_filtered == 1
        assert incidents == []


class TestNightAbsoluteFloor:
    """The night floor set lowers BOTH bars: on an elevated organic IF
    baseline (share 0.50) the wave's |observed - baseline| is 0.167 — under
    the global 25pp absolute-deviation floor but over the 15pp night one."""

    def _fixture(self, db_session, name: str) -> None:
        merchant = _merchant(db_session, name)
        _seed(
            db_session,
            merchant,
            {
                **{h: (2, 2) for h in range(2, 10)},  # baseline share 0.50
                18: (4, 2),  # night wave: share 0.667, deviation 0.167
                19: (4, 2),
                20: (4, 2),
            },
        )
        db_session.commit()

    def test_detected_with_mode_on(self, db_session):
        self._fixture(db_session, "night absdev on")

        result, incidents = _run(db_session, night_regime_floors=True)

        assert result.anomalies_detected == 1
        (incident,) = [i for i in incidents if i.metric == METRIC]
        assert incident.meta["night_regime_floors"] is True

    def test_silent_with_mode_off(self, db_session):
        self._fixture(db_session, "night absdev off")

        result, incidents = _run(db_session, night_regime_floors=False)

        assert result.anomalies_detected == 0
        assert result.anomalies_filtered == 1
        assert incidents == []


class TestDayBehaviorUnchanged:
    """The night floor set must never relax daytime judgment — the organic
    daytime clusters (0.71 share) that forced the 0.90 bar stay out in both
    modes, and a genuinely near-pure daytime hour still fires in both."""

    def test_day_cluster_under_the_global_bar_stays_silent_in_both_modes(self, db_session):
        merchant = _merchant(db_session, "day under bar")
        _seed(
            db_session,
            merchant,
            {
                **HEALTHY_BASELINE,
                10: (3, 1),  # daytime cluster: share 0.75 (> 0.60 night bar,
                11: (3, 1),  # < 0.90 global bar) — exactly the organic shape
                12: (3, 1),  # the global bar exists to suppress
                **{h: (1, 3) for h in range(13, 22)},
            },
        )
        db_session.commit()

        off_result, off_incidents = _run(db_session, night_regime_floors=False)
        on_result, on_incidents = _run(db_session, night_regime_floors=True)

        for result, incidents in ((off_result, off_incidents), (on_result, on_incidents)):
            assert result.anomalies_detected == 0
            assert result.anomalies_filtered == 1
            assert incidents == []

    def test_mixed_day_night_anomaly_faces_the_global_floors(self, db_session):
        """One daytime bucket in the episode disqualifies the night set: a
        0.667-share run spanning 16:00 (day) and 18:00 (night) is judged by
        the global 0.90 bar even with the mode on."""
        merchant = _merchant(db_session, "mixed span")
        _seed(
            db_session,
            merchant,
            {
                **HEALTHY_BASELINE,
                16: (4, 2),  # day
                17: (4, 2),  # day
                18: (4, 2),  # night — but the episode is not ALL-night
                **{h: (1, 3) for h in range(19, 22)},
            },
        )
        db_session.commit()

        result, incidents = _run(db_session, night_regime_floors=True)

        assert result.anomalies_detected == 0
        assert result.anomalies_filtered == 1
        assert incidents == []

    def test_day_near_pure_cluster_fires_with_mode_off(self, db_session):
        merchant = _merchant(db_session, "day pure off")
        _seed(
            db_session,
            merchant,
            {
                **HEALTHY_BASELINE,
                10: (5, 0),  # near-single-class daytime hour: share 1.0
                11: (5, 0),
                12: (5, 0),
                **{h: (1, 3) for h in range(13, 22)},
            },
        )
        db_session.commit()

        result, incidents = _run(db_session, night_regime_floors=False)

        assert result.anomalies_detected == 1
        (incident,) = [i for i in incidents if i.metric == METRIC]
        assert "night_regime_floors" not in incident.meta  # global floor path

    def test_day_near_pure_cluster_fires_with_mode_on(self, db_session):
        merchant = _merchant(db_session, "day pure on")
        _seed(
            db_session,
            merchant,
            {
                **HEALTHY_BASELINE,
                10: (5, 0),
                11: (5, 0),
                12: (5, 0),
                **{h: (1, 3) for h in range(13, 22)},
            },
        )
        db_session.commit()

        result, incidents = _run(db_session, night_regime_floors=True)

        assert result.anomalies_detected == 1
        (incident,) = [i for i in incidents if i.metric == METRIC]
        assert "night_regime_floors" not in incident.meta  # day: global floors


class TestConfig:
    def test_default_is_off(self):
        assert DetectionRunRequest().night_regime_floors is False

    def test_round_trip(self):
        on = DetectionRunRequest(night_regime_floors=True)
        assert on.night_regime_floors is True
        assert (
            DetectionRunRequest.model_validate(on.model_dump()).night_regime_floors
            is True
        )
        assert (
            DetectionRunRequest.model_validate_json(on.model_dump_json()).night_regime_floors
            is True
        )
        off = DetectionRunRequest()
        assert (
            DetectionRunRequest.model_validate(off.model_dump()).night_regime_floors
            is False
        )

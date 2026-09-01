"""Same-time-yesterday baseline mode (DetectionRunRequest.baseline_mode).

The default leading-window baseline compares each bucket against the window's
first healthy buckets, so a daily-cycle trough (same hour every day) looks
like a degradation — the seasonality false positive named in
docs/detection.md "Known limitations". The opt-in `same_time_yesterday` mode
instead builds the baseline from the SAME clock window shifted back 24h: a
daily dip compares against yesterday's dip and stays silent, while a genuine
degradation (healthy yesterday, degraded today) still fires.

Everything here is deterministic: exact terminal outcomes per bucket on an
explicit two-day grid (no randomness, no wall-clock dependence — the window
is anchored by an explicit `as_of`).
"""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

import app.models as models
from app.schemas.detection import DetectionRunRequest
from app.services.detection import run_detection

DAY0 = datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)  # "yesterday"
DAY1 = DAY0 + timedelta(days=1)  # "today"
BUCKET = timedelta(minutes=5)
PER_BUCKET = 10

# The analysis window: as_of anchors window_end = 02:05, so the pass covers
# [01:05, 02:05] — 13 five-minute buckets; the dip occupies the LAST 5
# buckets (01:45..02:05), after the 8-bucket leading baseline.
AS_OF = DAY1.replace(hour=2, minute=0)
DIP_START_HOUR, DIP_END_HOUR = 1 + 45 / 60, 2 + 5 / 60  # in each day's clock


def _dip_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.replace(hour=1, minute=45)
    end = day.replace(hour=2, minute=5)
    return start, end


def _seed_day(db_session, merchant, day: datetime, *, dip_rate: float | None) -> None:
    """Terminal outcomes 00:30..02:10 for one day: healthy success rate
    everywhere, with an optional exact dip to `dip_rate` in [01:45, 02:05)."""
    dip_start, dip_end = _dip_bounds(day)
    ts = day.replace(hour=0, minute=30)
    while ts < day.replace(hour=2, minute=10):
        rate = dip_rate if (dip_rate is not None and dip_start <= ts < dip_end) else 1.0
        n_success = round(PER_BUCKET * rate)
        for j in range(PER_BUCKET):
            ok = j < n_success
            status = "captured" if ok else "failed"
            payment = models.Payment(
                merchant_id=merchant.id,
                amount_paise=10_000,
                status=status,
                gateway_created_at=ts,
            )
            db_session.add(payment)
            db_session.flush()
            db_session.add(
                models.PaymentEvent(
                    payment_id=payment.id,
                    event_type=f"payment.{status}",
                    to_status=status,
                    source="seed",
                    payload={},
                    occurred_at=ts,
                )
            )
        ts += BUCKET


def _run(db_session, *, baseline_mode: str) -> tuple:
    result = run_detection(
        db_session,
        DetectionRunRequest(
            as_of=AS_OF,
            window_minutes=60,
            metrics=["payment_success_rate"],
            environment="research",
            baseline_mode=baseline_mode,
        ),
    )
    incidents = list(db_session.scalars(sa.select(models.Incident)))
    return result, incidents


class TestSeasonality:
    def test_daily_dip_is_a_false_positive_in_the_default_mode(self, db_session):
        """Control: the SAME fixture must fire under the leading-window
        baseline — otherwise the seasonality test below proves nothing."""
        merchant = models.Merchant(name="sty seasonal")
        db_session.add(merchant)
        db_session.flush()
        _seed_day(db_session, merchant, DAY0, dip_rate=0.5)  # yesterday had the dip too
        _seed_day(db_session, merchant, DAY1, dip_rate=0.5)
        db_session.commit()

        result, incidents = _run(db_session, baseline_mode="leading_window")

        assert result.anomalies_detected >= 1
        assert any(i.metric == "payment_success_rate" for i in incidents)

    def test_daily_dip_stays_silent_with_same_time_yesterday(self, db_session):
        merchant = models.Merchant(name="sty seasonal")
        db_session.add(merchant)
        db_session.flush()
        _seed_day(db_session, merchant, DAY0, dip_rate=0.5)
        _seed_day(db_session, merchant, DAY1, dip_rate=0.5)
        db_session.commit()

        result, incidents = _run(db_session, baseline_mode="same_time_yesterday")

        # Yesterday's same clock hours carried the same dip: the baseline IS
        # the dip, so the detector never fires (nothing even reaches the
        # noise floors).
        assert result.anomalies_detected == 0
        assert result.anomalies_filtered == 0
        assert incidents == []


class TestGenuineAnomalyStillFires:
    def test_degradation_with_a_healthy_yesterday_is_detected(self, db_session):
        merchant = models.Merchant(name="sty anomaly")
        db_session.add(merchant)
        db_session.flush()
        _seed_day(db_session, merchant, DAY0, dip_rate=None)  # yesterday healthy
        _seed_day(db_session, merchant, DAY1, dip_rate=0.4)  # today genuinely degraded
        db_session.commit()

        result, incidents = _run(db_session, baseline_mode="same_time_yesterday")

        assert result.anomalies_detected >= 1
        (incident,) = [i for i in incidents if i.metric == "payment_success_rate"]
        assert incident.baseline_value == 1.0  # yesterday's same-clock baseline
        assert incident.observed_value == 0.4
        assert incident.meta["baseline_mode"] == "same_time_yesterday"
        assert "baseline_mode=same_time_yesterday" in (result.detail or "")


class TestSparseYesterday:
    def test_no_yesterday_data_means_an_honest_silence(self, db_session):
        """With no decidable baseline buckets yesterday, the mode says
        nothing rather than guessing a baseline."""
        merchant = models.Merchant(name="sty sparse")
        db_session.add(merchant)
        db_session.flush()
        _seed_day(db_session, merchant, DAY1, dip_rate=0.4)  # today only
        db_session.commit()

        result, incidents = _run(db_session, baseline_mode="same_time_yesterday")

        assert result.anomalies_detected == 0
        assert incidents == []

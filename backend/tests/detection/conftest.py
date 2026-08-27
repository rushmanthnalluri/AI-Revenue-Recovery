"""Detection test fixtures: synthetic labeled series + payment_events seeding.

Everything here is self-contained and deterministic (seeded RNG / exact event
counts) so detector comparisons are reproducible. Series fixtures are SMALL on
purpose — they are unit-test fixtures, not the simulator; any numbers derived
from them are synthetic-fixture results, not production metrics.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Merchant, Payment, PaymentEvent
from app.services.detection.series import Bucket, floor_bucket

EPOCH = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)  # fixed, aligned


def make_sr_buckets(
    *,
    n: int = 60,
    base_rate: float = 0.9,
    count: int = 100,
    shift_at: int | None = None,
    shift_to: float | None = None,
    recover_at: int | None = None,
    drift_until: int | None = None,
    seed: int = 7,
) -> tuple[list[Bucket], set[int]]:
    """Success-rate series sampled binomially, with an optional injected
    degradation. Returns (buckets, labeled degraded bucket indices).

    - step shift: rate == shift_to for shift_at <= i < recover_at (or to end)
    - drift: linear ramp from base_rate at shift_at to shift_to at drift_until
    """
    rng = np.random.default_rng(seed)
    buckets: list[Bucket] = []
    labels: set[int] = set()
    for i in range(n):
        p = base_rate
        if shift_at is not None and i >= shift_at:
            if drift_until is not None:
                span = max(drift_until - shift_at, 1)
                frac = min((i - shift_at) / span, 1.0)
                p = base_rate + (shift_to - base_rate) * frac
                labels.add(i)
            elif recover_at is None or i < recover_at:
                p = shift_to
                labels.add(i)
        succ = int(rng.binomial(count, p))
        buckets.append(
            Bucket(ts=EPOCH + timedelta(minutes=5 * i), value=succ / count, count=count)
        )
    return buckets, labels


def make_latency_buckets(
    *,
    n: int = 60,
    base_ms: float = 250.0,
    noise_ms: float = 15.0,
    count: int = 50,
    spike_at: int | None = None,
    spike_ms: float = 1200.0,
    recover_at: int | None = None,
    seed: int = 11,
) -> tuple[list[Bucket], set[int]]:
    """Latency series (normal noise) with an optional injected spike."""
    rng = np.random.default_rng(seed)
    buckets: list[Bucket] = []
    labels: set[int] = set()
    for i in range(n):
        mu = base_ms
        if spike_at is not None and i >= spike_at and (recover_at is None or i < recover_at):
            mu = spike_ms
            labels.add(i)
        value = max(float(rng.normal(mu, noise_ms)), 1.0)
        buckets.append(
            Bucket(ts=EPOCH + timedelta(minutes=5 * i), value=value, count=count)
        )
    return buckets, labels


@pytest.fixture()
def sr_series():
    return make_sr_buckets


@pytest.fixture()
def latency_series():
    return make_latency_buckets


# ---------------------------------------------------------------------------
# DB seeding: exact terminal outcomes per bucket (no randomness)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stream:
    """One homogeneous traffic stream inside each bucket (e.g. all UPI
    payments). Rates are realized exactly: round(per_bucket * rate) successes."""

    method: str = "card"
    bank: str = "hdfc"
    per_bucket: int = 20
    rate_at: Callable[[int], float] = lambda i: 0.9
    latency_at: Callable[[int], float | None] = lambda i: None


@pytest.fixture()
def seed_payment_events(db_session: Session):
    """Seed a merchant + payments + terminal payment_events on a time grid.

    ``streams`` are realized in every bucket, so per-segment degradation is
    expressed exactly (e.g. UPI collapses while card stays healthy). The grid
    ends at utcnow() so the detection window (anchored at the latest event)
    covers the whole fixture.
    """

    def _seed(
        *,
        buckets: int = 48,
        bucket_minutes: int = 5,
        streams: list[Stream] | None = None,
        amount_paise: int = 10000,
    ) -> Merchant:
        streams = streams or [Stream()]
        merchant = Merchant(name="Detection Test Merchant")
        db_session.add(merchant)
        db_session.flush()

        step = timedelta(minutes=bucket_minutes)
        start = floor_bucket(utcnow(), bucket_minutes) - buckets * step
        for i in range(buckets):
            ts = start + i * step
            for stream in streams:
                n_success = round(stream.per_bucket * stream.rate_at(i))
                latency = stream.latency_at(i)
                for j in range(stream.per_bucket):
                    ok = j < n_success
                    status = "captured" if ok else "failed"
                    payment = Payment(
                        merchant_id=merchant.id,
                        amount_paise=amount_paise,
                        status=status,
                        method=stream.method,
                        gateway_created_at=ts,
                        meta={"bank": stream.bank, "gateway": "razorpay"},
                    )
                    db_session.add(payment)
                    db_session.flush()
                    payload = {"latency_ms": latency} if (ok and latency is not None) else {}
                    db_session.add(
                        PaymentEvent(
                            payment_id=payment.id,
                            event_type=f"payment.{status}",
                            to_status=status,
                            source="seed",
                            payload=payload,
                            occurred_at=ts + timedelta(seconds=30),
                        )
                    )
        db_session.commit()
        return merchant

    return _seed

"""Seeded sampling helpers: weighted choice, seasonality, amounts, latencies.

All randomness flows through one ``random.Random`` instance owned by the
engine; draws happen in a fixed code path, so a given (seed, config) pair
always produces the same dataset. ``random.Random`` is deterministic across
platforms and Python runs for the same seed.

Time-of-day / day-of-week seasonality is defined in **IST** (the merchant's
customers are Indian) and converted to tz-aware UTC timestamps by the engine.
"""

import bisect
import math
import random
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Seasonality (IST)
# ---------------------------------------------------------------------------

# Relative checkout volume by hour of day (IST). Lunch + late-evening peaks.
HOURLY_WEIGHTS_IST: tuple[float, ...] = (
    0.25, 0.12, 0.07, 0.05, 0.05, 0.08,  # 00-05 night trough
    0.15, 0.25, 0.40, 0.60, 0.85, 1.00,  # 06-11 morning ramp
    0.95, 0.90, 0.85, 0.80, 0.75, 0.70,  # 12-17 afternoon
    0.80, 1.00, 1.10, 1.15, 1.00, 0.60,  # 18-23 evening peak
)

# Day-of-week multiplier, Monday..Sunday (weekend uplift).
DOW_WEIGHTS: tuple[float, ...] = (0.95, 0.97, 1.00, 1.02, 1.05, 1.18, 1.12)

# Success rates dip slightly in the small hours (issuer maintenance windows).
NIGHT_SUCCESS_FACTOR = 0.97  # applied 00:00-05:59 IST

IST_OFFSET_SECONDS = 5 * 3600 + 30 * 60  # UTC+5:30

# ---------------------------------------------------------------------------
# Amounts (integer paise) and latencies (ms) — lognormal-ish
# ---------------------------------------------------------------------------

AMOUNT_MEDIAN_PAISE = 49_900  # ₹499
AMOUNT_SIGMA = 0.95
AMOUNT_MIN_PAISE = 10_000  # ₹100
AMOUNT_MAX_PAISE = 5_000_000  # ₹50,000

# Method-level amount shape: netbanking skews large, wallets small.
AMOUNT_METHOD_FACTOR: dict[str, float] = {
    "card": 1.0,
    "upi": 0.85,
    "netbanking": 1.40,
    "wallet": 0.40,
}

# Median gateway latency per method (ms) + lognormal sigma.
LATENCY_PARAMS: dict[str, tuple[float, float]] = {
    "card": (2_800.0, 0.50),
    "upi": (9_500.0, 0.60),  # collect flow waits on customer approval
    "netbanking": (14_000.0, 0.50),
    "wallet": (2_200.0, 0.45),
}
LATENCY_MIN_MS = 300
LATENCY_MAX_MS = 120_000


class WeightedChoice:
    """Precomputed cumulative-weight sampler (O(log n) per draw)."""

    def __init__(self, items_weights: Sequence[tuple[T, float]]):
        self._items: list[T] = []
        self._cum: list[float] = []
        total = 0.0
        for item, w in items_weights:
            total += w
            self._items.append(item)
            self._cum.append(total)
        self._total = total

    def pick(self, rng: random.Random) -> T:
        x = rng.random() * self._total
        return self._items[bisect.bisect_left(self._cum, x)]


class WeightedIndex:
    """Same as WeightedChoice but returns the index (for parallel arrays)."""

    def __init__(self, weights: Sequence[float]):
        self._cum: list[float] = []
        total = 0.0
        for w in weights:
            total += w
            self._cum.append(total)
        self._total = total

    def pick(self, rng: random.Random) -> int:
        x = rng.random() * self._total
        return bisect.bisect_left(self._cum, x)


def sample_amount_paise(rng: random.Random, method: str) -> int:
    """Lognormal amount in paise, median ~₹499, clipped to [₹100, ₹50,000],
    rounded to whole rupees like a real checkout."""
    mu = math.log(AMOUNT_MEDIAN_PAISE * AMOUNT_METHOD_FACTOR[method])
    v = rng.lognormvariate(mu, AMOUNT_SIGMA)
    v = min(max(v, AMOUNT_MIN_PAISE), AMOUNT_MAX_PAISE)
    return int(round(v / 100.0)) * 100


def sample_latency_ms(rng: random.Random, method: str) -> int:
    median, sigma = LATENCY_PARAMS[method]
    v = rng.lognormvariate(math.log(median), sigma)
    return int(min(max(v, LATENCY_MIN_MS), LATENCY_MAX_MS))


def ist_hour_to_utc_hour(hour_ist: float) -> float:
    """Map an IST wall-clock hour to the corresponding UTC hour offset."""
    return (hour_ist - 5.5) % 24.0

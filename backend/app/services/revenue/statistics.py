"""Uncertainty math for the revenue engine — deliberately simple.

The only probabilistic quantity in the methodology is a binomial success
rate (baseline payment success per segment). Everything else is deterministic
multiplication, so all uncertainty propagation reduces to: evaluate the
formula at the rate's confidence-interval bounds and carry the paise.

We use the Wilson score interval because, unlike the normal ("Wald")
approximation, it behaves sanely for small n and extreme rates (p near 0 or
1) — exactly the regime payment segments live in — and it is one formula with
no distribution lookup. With n == 0 it degrades honestly to [0, 1].

Reference: Wilson, E.B. (1927); standard coverage properties summarized in
Brown, Cai, DasGupta (2001), "Interval Estimation for a Binomial Proportion".
"""

import math


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Returns (lower, upper), always within [0, 1]. For n == 0 there is no
    information, so the interval is the full [0, 1] — callers must surface
    that as a wide band, never as a point.
    """
    if n <= 0:
        return (0.0, 1.0)
    successes = min(max(successes, 0), n)
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def rate_confidence(n: int, full_confidence_sample: int) -> float:
    """Sample-size-driven confidence in a rate estimate, in [0, 1].

    Linear ramp: 0 observations -> 0.0, `full_confidence_sample` or more -> 1.0.
    Kept intentionally crude: it communicates "how much data backs this" to the
    dashboard, it is not a frequentist guarantee (the Wilson band is that).
    """
    if n <= 0 or full_confidence_sample <= 0:
        return 0.0
    return min(1.0, n / full_confidence_sample)

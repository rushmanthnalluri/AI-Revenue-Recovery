"""Unit tests for holdout assignment and the lift confidence intervals.

Properties under test (pre-registered design, docs/product-strategy.md §4.1):
- assignment is deterministic per (seed, customer id) and seed-dependent;
- realized membership tracks the configured fraction within tolerance;
- Wilson/Newcombe intervals are exact on known values, never crash on tiny
  counts, and always bracket the point estimate.
"""

from app.services.evaluation.holdout import (
    DEFAULT_HOLDOUT_FRACTION,
    holdout_token,
    is_holdout,
    median,
    newcombe_ci,
    wilson_interval,
)


def test_token_is_deterministic_and_in_unit_interval():
    a = holdout_token(42, "cust_0001")
    b = holdout_token(42, "cust_0001")
    assert a == b and 0.0 <= a < 1.0
    # Different customer (almost always) → different token.
    assert holdout_token(42, "cust_0002") != a


def test_membership_tracks_fraction_within_tolerance():
    customers = [f"cust_{i:05d}" for i in range(20_000)]
    for fraction, tol in ((0.10, 0.015), (0.25, 0.02), (0.05, 0.012)):
        share = sum(is_holdout(42, fraction, c) for c in customers) / len(customers)
        assert abs(share - fraction) < tol, (fraction, share)


def test_membership_is_identical_for_same_seed_and_seed_dependent():
    customers = [f"cust_{i:04d}" for i in range(500)]
    first = {c for c in customers if is_holdout(7, 0.2, c)}
    again = {c for c in customers if is_holdout(7, 0.2, c)}
    other_seed = {c for c in customers if is_holdout(8, 0.2, c)}
    assert first == again
    assert first != other_seed


def test_uncustomerable_payments_stay_in_treatment():
    assert is_holdout(42, 0.999, None) is False
    assert is_holdout(42, 0.0, "cust_x") is False


def test_default_fraction_is_within_preregistered_band():
    # docs/product-strategy.md §4.1: 5-10%.
    assert 0.05 <= DEFAULT_HOLDOUT_FRACTION <= 0.10


def test_wilson_known_values():
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0
    assert hi == 0.27753279995699603  # z = 1.959963985
    lo, hi = wilson_interval(10, 10)
    assert hi == 1.0
    assert round(lo, 5) == 0.72247
    lo, hi = wilson_interval(5, 10)
    assert round(lo, 5) == 0.23659
    assert round(hi, 5) == 0.76341


def test_wilson_degenerate_inputs_never_crash():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    lo, hi = wilson_interval(0, 3)
    assert lo == 0.0 and hi > 0.5  # tiny n, zero successes -> wide band


def test_newcombe_brackets_point_on_tiny_counts():
    for args in ((0, 5, 0, 7), (1, 3, 0, 2), (0, 1, 0, 1), (2, 2, 2, 2)):
        t_ok, t_n, h_ok, h_n = args
        point = (t_ok / t_n if t_n else 0.0) - (h_ok / h_n if h_n else 0.0)
        low, high = newcombe_ci(*args)
        assert low <= point <= high, args
        assert -1.0 <= low and high <= 1.0
        assert high > low  # never a bare point estimate


def test_newcombe_all_zero_is_total_ignorance():
    assert newcombe_ci(0, 0, 0, 0) == (-1.0, 1.0)


def test_newcombe_detects_clear_separation():
    low, high = newcombe_ci(9, 10, 1, 10)
    assert low > 0.0  # the whole band sits above no effect
    assert high < 1.0


def test_median_odd_even_empty():
    assert median([]) is None
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert median([4.0, 1.0, 3.0, 2.0]) == 2.5

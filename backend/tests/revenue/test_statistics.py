"""CI sanity: Wilson interval properties, confidence ramp, Estimate rounding."""

import pytest

from app.services.revenue.statistics import rate_confidence, wilson_interval
from app.services.revenue.types import Estimate


def test_wilson_zero_n_is_full_unit_interval():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    assert wilson_interval(5, 0) == (0.0, 1.0)  # nonsense input -> no information


def test_wilson_known_value():
    # Reference value for 50/100 at z=1.96 (standard textbook computation).
    lo, hi = wilson_interval(50, 100, 1.96)
    assert lo == pytest.approx(0.4038, abs=1e-3)
    assert hi == pytest.approx(0.5962, abs=1e-3)


@pytest.mark.parametrize(
    "k,n",
    [(0, 10), (10, 10), (1, 3), (360, 400), (7, 9), (0, 1), (1, 1)],
)
def test_wilson_bounds_stay_in_unit_interval(k, n):
    lo, hi = wilson_interval(k, n)
    assert 0.0 <= lo <= hi <= 1.0


@pytest.mark.parametrize("k,n", [(0, 10), (10, 10), (3, 10), (90, 100)])
def test_wilson_contains_mle(k, n):
    lo, hi = wilson_interval(k, n)
    assert lo <= k / n <= hi


def test_wilson_shrinks_with_more_data():
    narrow = wilson_interval(500, 1000)
    wide = wilson_interval(50, 100)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_wilson_handles_extreme_rates_without_wald_pathology():
    # Wald would produce [1,1] for 10/10; Wilson stays honest.
    lo, hi = wilson_interval(10, 10)
    assert lo < 1.0 and hi == 1.0
    lo0, hi0 = wilson_interval(0, 10)
    assert lo0 == 0.0 and hi0 > 0.0


def test_rate_confidence_ramp():
    assert rate_confidence(0, 200) == 0.0
    assert rate_confidence(100, 200) == pytest.approx(0.5)
    assert rate_confidence(200, 200) == 1.0
    assert rate_confidence(10_000, 200) == 1.0  # saturates
    assert rate_confidence(10, 0) == 0.0  # degenerate config guard


def test_estimate_scale_rounds_outward():
    est = Estimate(
        point_paise=11, lower_paise=9, upper_paise=11, confidence=0.8, low_confidence=False
    )
    scaled = est.scale(0.5)
    assert scaled.point_paise == 6  # 11 * 0.5 = 5.5 -> round-half-even 6
    assert scaled.lower_paise == 4  # floor(4.5) — bands round outward
    assert scaled.upper_paise == 6  # ceil(5.5)
    assert scaled.confidence == est.confidence
    assert scaled.low_confidence == est.low_confidence


def test_estimate_scale_preserves_none_point():
    est = Estimate(
        point_paise=None, lower_paise=0, upper_paise=1000, confidence=0.0, low_confidence=True
    )
    scaled = est.scale(0.7)
    assert scaled.point_paise is None
    assert scaled.upper_paise == 700


def test_estimate_zero():
    z = Estimate.zero("nothing here")
    assert (z.point_paise, z.lower_paise, z.upper_paise) == (0, 0, 0)
    assert z.low_confidence is True

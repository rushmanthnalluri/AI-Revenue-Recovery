"""Noise floors for the insights engine (Watchdog-style min-traffic gates).

The floors exist so tiny samples can never dress up as signal: below the hard
support floor a facet is suppressed entirely; between the hard floor and the
confident floor it is listed but marked ``low_confidence``. Lift alone is never
enough — an absolute delta floor per basis stops near-zero baselines from
producing meaningless "×4" outliers.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class InsightsConfig:
    # Hard suppression floor: fewer window failures than this -> not listed.
    min_support: int = 3
    # Below this a listed facet is marked low_confidence (real, but thin).
    confident_support: int = 10
    # Overrepresentation floor: incident rate must be at least this multiple of
    # the baseline rate. Facets absent at baseline (lift is None) pass.
    min_lift: float = 1.5
    # Absolute delta floors so tiny bases cannot pass on lift alone:
    min_rate_delta: float = 0.05  # failure_rate basis: +5 percentage points
    min_share_delta: float = 0.10  # failure_share basis: +10 percentage points
    # Cap on listed outliers, applied after deterministic ranking.
    max_outliers: int = 10


DEFAULT_CONFIG = InsightsConfig()

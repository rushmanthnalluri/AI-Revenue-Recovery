"""Decline-outlier insights for incidents (Datadog Watchdog + Pagos patterns).

For one incident's window this package ranks failure *facets* — method, bank,
gateway, error_code, error_reason — by overrepresentation versus the
pre-incident baseline window, and benchmarks the top facet against
whole-platform same-window behavior (merchant-vs-network callout).

Leaf reader by architecture rule (ADR 0010): models/db only, read-only, never
on the mutation path. See docs/product-strategy.md §4.2 for the product
rationale.
"""

from app.services.insights.config import DEFAULT_CONFIG, InsightsConfig
from app.services.insights.service import InsightsError, InsightsService
from app.services.insights.types import (
    BASIS_FAILURE_RATE,
    BASIS_FAILURE_SHARE,
    CLASS_INCIDENT_SPECIFIC,
    CLASS_PLATFORM_WIDE,
    SCOPE_SIMULATED_FLEET,
    ComputedFrom,
    FacetOutlier,
    IncidentInsights,
    PlatformCallout,
)

__all__ = [
    "BASIS_FAILURE_RATE",
    "BASIS_FAILURE_SHARE",
    "CLASS_INCIDENT_SPECIFIC",
    "CLASS_PLATFORM_WIDE",
    "DEFAULT_CONFIG",
    "SCOPE_SIMULATED_FLEET",
    "ComputedFrom",
    "FacetOutlier",
    "IncidentInsights",
    "InsightsConfig",
    "InsightsError",
    "InsightsService",
    "PlatformCallout",
]

"""Insights service: rank failure facets by overrepresentation, then benchmark
the top facet against whole-platform same-window behavior.

Formulas (per facet value v, incident window W vs equal-duration baseline B):

- group facets (method/bank/gateway, basis=failure_rate):
    incident_rate = failures_W(v) / payments_W(v)
    baseline_rate = failures_B(v) / payments_B(v)
- error facets (error_code/error_reason, basis=failure_share):
    incident_rate = failures_W(v) / failures_W(all)
    baseline_rate = failures_B(v) / failures_B(all)
- lift = incident_rate / baseline_rate (None when baseline_rate == 0 — the
  facet is new; that ranks above any finite lift).

A facet is listed only when it clears every floor (InsightsConfig): support >=
min_support, lift >= min_lift (unless new), and an absolute rate/share delta
floor. support < confident_support is listed but marked low_confidence.

Ranking is deterministic: new-at-baseline first, then lift desc, support
desc, (dimension, value) asc. Same data -> same ranking.
"""

from sqlalchemy.orm import Session

from app.models import Incident
from app.services.insights.config import DEFAULT_CONFIG, InsightsConfig
from app.services.insights.facets import (
    ERROR_DIMENSIONS,
    GROUP_DIMENSIONS,
    UNKNOWN,
    FacetOutcome,
    incident_windows,
    load_outcomes,
    restrict_to_segment,
)
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


class InsightsError(Exception):
    """Raised when insights cannot be computed for an incident."""


def _rate(num: int, den: int) -> float:
    return float(num) / float(den) if den > 0 else 0.0


def _group_stats(outcomes: list[FacetOutcome], dim: str) -> dict[str, list[int]]:
    """value -> [payments, failures] over all outcomes (group facets)."""
    stats: dict[str, list[int]] = {}
    for o in outcomes:
        value = o.facets.get(dim) or UNKNOWN
        cell = stats.setdefault(value, [0, 0])
        cell[0] += 1
        if not o.success:
            cell[1] += 1
    return stats


def _failure_counts(outcomes: list[FacetOutcome], dim: str) -> dict[str, int]:
    """value -> failure count over failed outcomes only (error facets)."""
    counts: dict[str, int] = {}
    for o in outcomes:
        if o.success:
            continue
        value = o.facets.get(dim) or UNKNOWN
        counts[value] = counts.get(value, 0) + 1
    return counts


def _candidate(
    config: InsightsConfig,
    *,
    dimension: str,
    value: str,
    basis: str,
    incident_rate: float,
    baseline_rate: float,
    support: int,
    window_group_size: int,
    baseline_group_size: int,
) -> FacetOutlier | None:
    """Apply the noise floors; None = suppressed as noise."""
    if support < config.min_support:
        return None
    delta = incident_rate - baseline_rate
    floor = (
        config.min_rate_delta if basis == BASIS_FAILURE_RATE else config.min_share_delta
    )
    if delta < floor:
        return None
    lift = incident_rate / baseline_rate if baseline_rate > 0 else None
    if lift is not None and lift < config.min_lift:
        return None
    return FacetOutlier(
        dimension=dimension,
        value=value,
        basis=basis,
        incident_rate=round(incident_rate, 6),
        baseline_rate=round(baseline_rate, 6),
        lift=round(lift, 4) if lift is not None else None,
        support=support,
        window_group_size=window_group_size,
        baseline_group_size=baseline_group_size,
        low_confidence=support < config.confident_support,
    )


def _rank_key(outlier: FacetOutlier) -> tuple:
    # New-at-baseline (lift None) ranks above any finite lift; then support,
    # then a stable name tie-break. Fully deterministic.
    return (
        0 if outlier.lift is None else 1,
        -(outlier.lift or 0.0),
        -outlier.support,
        outlier.dimension,
        outlier.value,
    )


def rank_outliers(
    window: list[FacetOutcome],
    baseline: list[FacetOutcome],
    config: InsightsConfig = DEFAULT_CONFIG,
) -> list[FacetOutlier]:
    """Rank overrepresented failure facets (pure — shared by service + tests)."""
    w_failures = sum(1 for o in window if not o.success)
    b_failures = sum(1 for o in baseline if not o.success)
    outliers: list[FacetOutlier] = []

    for dim in GROUP_DIMENSIONS:
        w_stats = _group_stats(window, dim)
        b_stats = _group_stats(baseline, dim)
        for value in sorted(w_stats):
            nw, fw = w_stats[value]
            if fw == 0:
                continue
            nb, fb = b_stats.get(value, [0, 0])
            candidate = _candidate(
                config,
                dimension=dim,
                value=value,
                basis=BASIS_FAILURE_RATE,
                incident_rate=_rate(fw, nw),
                baseline_rate=_rate(fb, nb),
                support=fw,
                window_group_size=nw,
                baseline_group_size=nb,
            )
            if candidate is not None:
                outliers.append(candidate)

    for dim in ERROR_DIMENSIONS:
        w_counts = _failure_counts(window, dim)
        b_counts = _failure_counts(baseline, dim)
        for value in sorted(w_counts):
            cw = w_counts[value]
            candidate = _candidate(
                config,
                dimension=dim,
                value=value,
                basis=BASIS_FAILURE_SHARE,
                incident_rate=_rate(cw, w_failures),
                baseline_rate=_rate(b_counts.get(value, 0), b_failures),
                support=cw,
                window_group_size=w_failures,
                baseline_group_size=b_failures,
            )
            if candidate is not None:
                outliers.append(candidate)

    outliers.sort(key=_rank_key)
    return outliers[: config.max_outliers]


def _platform_rates(
    top: FacetOutlier,
    fleet_window: list[FacetOutcome],
    fleet_baseline: list[FacetOutcome],
) -> tuple[float, float, int]:
    """(window_rate, baseline_rate, support) for the facet over the whole fleet."""
    if top.basis == BASIS_FAILURE_RATE:
        nw, fw = _group_stats(fleet_window, top.dimension).get(top.value, [0, 0])
        nb, fb = _group_stats(fleet_baseline, top.dimension).get(top.value, [0, 0])
        return _rate(fw, nw), _rate(fb, nb), fw
    w_fail = [o for o in fleet_window if not o.success]
    b_fail = [o for o in fleet_baseline if not o.success]
    cw = _failure_counts(w_fail, top.dimension).get(top.value, 0)
    cb = _failure_counts(b_fail, top.dimension).get(top.value, 0)
    return _rate(cw, len(w_fail)), _rate(cb, len(b_fail)), cw


def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def build_callout(
    top: FacetOutlier,
    fleet_window: list[FacetOutcome],
    fleet_baseline: list[FacetOutcome],
    config: InsightsConfig = DEFAULT_CONFIG,
) -> PlatformCallout:
    """Benchmark the top outlier against the whole fleet in the same windows.

    Elevated fleet-wide -> consistent with a rail-side (network) cause;
    elevated only inside the incident's segment -> merchant-specific pattern.
    """
    p_window, p_baseline, p_support = _platform_rates(top, fleet_window, fleet_baseline)
    p_lift = p_window / p_baseline if p_baseline > 0 else None
    floor = (
        config.min_rate_delta
        if top.basis == BASIS_FAILURE_RATE
        else config.min_share_delta
    )
    elevated = (
        p_support >= config.min_support
        and (p_window - p_baseline) >= floor
        and (p_lift is None or p_lift >= config.min_lift)
    )

    label = f"{top.dimension}={top.value}"
    rates_txt = f"{_pct(round(p_baseline, 6))} -> {_pct(round(p_window, 6))}"
    if elevated:
        lift_txt = f", lift x{p_lift:.1f}" if p_lift is not None else ", absent at baseline"
        summary = (
            f"{label} failures are elevated across the whole simulated fleet in this "
            f"window ({rates_txt}{lift_txt}) — consistent with a rail-side cause, "
            f"not specific to this incident's segment."
        )
        classification = CLASS_PLATFORM_WIDE
    else:
        summary = (
            f"{label} failures are elevated only inside this incident's segment; "
            f"fleet-wide the same facet is near baseline ({rates_txt}) — a "
            f"merchant-specific pattern, not visible platform-wide."
        )
        classification = CLASS_INCIDENT_SPECIFIC

    return PlatformCallout(
        dimension=top.dimension,
        value=top.value,
        classification=classification,
        platform_scope=SCOPE_SIMULATED_FLEET,
        platform_window_rate=round(p_window, 6),
        platform_baseline_rate=round(p_baseline, 6),
        platform_lift=round(p_lift, 4) if p_lift is not None else None,
        platform_support=p_support,
        summary=summary,
    )


class InsightsService:
    """Read-only decline-outlier analytics over the shared models."""

    def __init__(self, session: Session, config: InsightsConfig = DEFAULT_CONFIG) -> None:
        self._session = session
        self._cfg = config

    def incident_insights(self, incident_id: str) -> IncidentInsights:
        incident = self._session.get(Incident, incident_id)
        if incident is None:
            raise InsightsError(f"incident not found: {incident_id!r}")
        try:
            b_start, b_end, w_start, w_end = incident_windows(incident)
        except ValueError as exc:
            raise InsightsError(str(exc)) from exc

        segment = {
            str(k): str(v)
            for k, v in ((incident.meta or {}).get("segment") or {}).items()
        }

        fleet_window = load_outcomes(self._session, w_start, w_end)
        fleet_baseline = load_outcomes(self._session, b_start, b_end)
        window = restrict_to_segment(fleet_window, segment)
        baseline = restrict_to_segment(fleet_baseline, segment)

        outliers = rank_outliers(window, baseline, self._cfg)
        callout = (
            build_callout(outliers[0], fleet_window, fleet_baseline, self._cfg)
            if outliers
            else None
        )

        return IncidentInsights(
            outliers=outliers,
            platform_callout=callout,
            computed_from=ComputedFrom(
                window_start=w_start,
                window_end=w_end,
                baseline_start=b_start,
                baseline_end=b_end,
                segment=segment,
                window_payments=len(window),
                window_failures=sum(1 for o in window if not o.success),
                baseline_payments=len(baseline),
                baseline_failures=sum(1 for o in baseline if not o.success),
            ),
        )


__all__ = [
    "InsightsError",
    "InsightsService",
    "build_callout",
    "rank_outliers",
]
